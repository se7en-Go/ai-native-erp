"""Track 4: tenant-isolated MCP Streamable HTTP server over `ErpToolRegistry`.

These tests run against real PostgreSQL 17 with `FORCE ROW LEVEL SECURITY`, so the
isolation guarantees are proven by the database, not by application filters:

* a Tenant A token can only ever read Tenant A SKUs, orders, receivables and
  analytics rows — Tenant B's records are invisible and unresolvable;
* only the SHA-256 digest and a short display prefix are persisted;
* `mcp:read` cannot reach the proposal-draft tool, an `allowed_tools` whitelist
  narrows `tools/list` and rejects out-of-whitelist calls, and unknown methods
  answer `-32601`;
* every `tools/call` writes an `ai_agent` audit event, and a draft proposal only
  accepts provenance that maps to a real earlier audited call of the same token;
* revoked/expired tokens are rejected with HTTP 401 and AGENT cannot mint tokens.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from erp_core.api_main import create_app
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_runtime_engine,
    create_session_factory,
    system_session,
    tenant_session,
)
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
MCP_URL = "/api/v1/mcp"
TOKENS_URL = "/api/v1/ai/mcp/tokens"
CATALOG_URL = "/api/v1/ai/mcp/tools"

#: Catalog order as declared by `mcp_tools.MCP_TOOL_SPECS`.
_READ_TOOL_NAMES = [
    "get_live_sku_pricing_and_stock",
    "get_live_order_tracking",
    "calc_b2b_quote_matrix",
    "match_open_receivables",
    "get_live_channel_sales_analytics",
    "query_readonly_analytics_sql",
]
_ALL_TOOL_NAMES = [*_READ_TOOL_NAMES, "create_l1_proposal_draft"]


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------


async def _seed_tenant(
    factory: Factory,
    *,
    label: str,
    order_no: str,
    sku: str,
    order_total: str,
    fx_rate: str,
    gmv_ad_spend: str,
) -> dict[str, UUID]:
    """Seed one tenant with a priced SKU, one order, tracking, a receivable and analytics."""
    ids = {
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "customer_id": uuid4(),
        "product_id": uuid4(),
        "variant_id": uuid4(),
        "warehouse_id": uuid4(),
        "order_id": uuid4(),
        "shipment_id": uuid4(),
        "receivable_id": uuid4(),
        "channel_account_id": uuid4(),
    }
    tid = ids["tenant_id"]
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'USD')"
            ),
            {"id": tid, "slug": f"mcp-{label}-{tid.hex[:8]}", "name": f"MCP {label} Co"},
        )
        await session.execute(
            text(
                "UPDATE tenants SET ai_policy_settings = CAST(:policy AS jsonb) WHERE id = :id"
            ),
            {
                "id": tid,
                "policy": '{"quote_pricing": {"usd_to_currency": {"CNY": "7.2"}, '
                '"incoterm_surcharge_pct": {"FOB": "0"}}}',
            },
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, :name, 'hash')"
            ),
            {
                "id": ids["user_id"],
                "email": f"owner@{label}-{tid.hex}@mcp-track4.example.com",
                "name": f"MCP owner {label}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tid, "uid": ids["user_id"]},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2b_contact', :name)"
            ),
            {"id": ids["customer_id"], "tid": tid, "name": f"{label} Buyer"},
        )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category, hs_code, certifications) "
                "VALUES (:id, :tid, :spu, 'Floodlight', '9405429000', ARRAY['CE','RoHS'])"
            ),
            {"id": ids["product_id"], "tid": tid, "spu": f"SPU-{label}-400"},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants "
                "(id, tenant_id, product_id, sku, cost_price_base, base_currency, "
                "min_margin_floor_pct, retail_price_usd, b2b_moq_tiers, weight_grams, "
                "lead_time_days) "
                "VALUES (:id, :tid, :pid, :sku, 68.0000, 'CNY', 15.00, 29.00, "
                "'[{\"min_qty\": 50, \"unit_price_usd\": \"18.50\"}, "
                "{\"min_qty\": 500, \"unit_price_usd\": \"14.20\"}]'::jsonb, 3200, 7)"
            ),
            {"id": ids["variant_id"], "tid": tid, "pid": ids["product_id"], "sku": sku},
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 1200, 150)"
            ),
            {"tid": tid, "wid": ids["warehouse_id"], "vid": ids["variant_id"]},
        )
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                "VALUES (:id, :tid, 'shopify', :ext, :name, 'active')"
            ),
            {
                "id": ids["channel_account_id"],
                "tid": tid,
                "ext": f"ext-{label}-{tid.hex[:8]}",
                "name": f"{label} Shopify",
            },
        )
        await session.execute(
            text(
                "INSERT INTO orders "
                "(id, tenant_id, order_no, order_mode, channel_account_id, customer_id, "
                "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                "fulfillment_status, payment_status) "
                "VALUES (:id, :tid, :order_no, 'b2c_retail', :caid, :cid, 'USD', :fx, "
                ":total, :total, 'shipped', 'paid')"
            ),
            {
                "id": ids["order_id"],
                "tid": tid,
                "order_no": order_no,
                "caid": ids["channel_account_id"],
                "cid": ids["customer_id"],
                "fx": fx_rate,
                "total": order_total,
            },
        )
        await session.execute(
            text(
                "INSERT INTO shipments "
                "(id, tenant_id, order_id, tracking_provider, tracking_number, carrier_code, "
                "standardized_status, sub_status, last_event_at, tracking_events) "
                "VALUES (:id, :tid, :oid, '17track', :tracking, 'yunexpress', "
                "'InTransit', 'InTransit_Customs', :ts, "
                "'[{\"time\": \"2026-11-20T10:00:00Z\", \"description\": \"Arrived\"}]'::jsonb)"
            ),
            {
                "id": ids["shipment_id"],
                "tid": tid,
                "oid": ids["order_id"],
                "tracking": f"LP{label.upper()}123456CN",
                "ts": datetime.now(UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO order_profit_snapshots "
                "(order_id, tenant_id, base_currency, gross_revenue_base, cogs_base, "
                "platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base, "
                "channel_msg_cost_base) "
                "VALUES (:oid, :tid, 'USD', 417.60, 136.00, 20.00, 12.00, 45.00, 0.00)"
            ),
            {"oid": ids["order_id"], "tid": tid},
        )
        await session.execute(
            text(
                "INSERT INTO channel_daily_metrics "
                "(tenant_id, channel_account_id, variant_id, metric_date, impressions, "
                "clicks, ad_spend_base, external_sessions, source) "
                "VALUES (:tid, :caid, NULL, CURRENT_DATE, 5000, 250, :ad_spend, 240, 'manual')"
            ),
            {"tid": tid, "caid": ids["channel_account_id"], "ad_spend": gmv_ad_spend},
        )
        await session.execute(
            text(
                "INSERT INTO receivables_payables "
                "(id, tenant_id, order_id, direction, milestone_label, due_amount, paid_amount, "
                "currency, status) "
                "VALUES (:id, :tid, :oid, 'receivable', 'b2b_30_deposit', 2130.00, 0.00, "
                "'USD', 'open')"
            ),
            {"id": ids["receivable_id"], "tid": tid, "oid": ids["order_id"]},
        )
    return ids


async def _cleanup_tenants(factory: Factory, tenants: list[dict[str, UUID]]) -> None:
    """Delete tenant-scoped rows as `app_runtime` in FK-safe order.

    `products` / `product_variants` do not cascade from `tenants`, so the ERP graph is
    unwound explicitly; `audit_events` and the remaining children are removed by the
    `tenants` cascade.
    """
    for ids in tenants:
        async with tenant_session(factory, ids["tenant_id"]) as session:
            for statement in (
                "DELETE FROM mcp_api_tokens WHERE tenant_id = :tid",
                "DELETE FROM ai_proposals WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM shipments WHERE tenant_id = :tid",
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM channel_daily_metrics WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
                "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
            ):
                await session.execute(text(statement), {"tid": ids["tenant_id"]})
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": ids["tenant_id"]}
            )
    async with system_session(factory) as session:
        for ids in tenants:
            await session.execute(
                text("DELETE FROM users WHERE id = :uid"), {"uid": ids["user_id"]}
            )


@pytest.fixture
async def runtime_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def mcp_env(runtime_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    factory = create_session_factory(runtime_engine)
    tenant_a = await _seed_tenant(
        factory,
        label="a",
        order_no="MCP-ORD-A-1001",
        sku="MCP-SKU-A",
        order_total="58.00",
        fx_rate="7.200000",
        gmv_ad_spend="100.00",
    )
    tenant_b = await _seed_tenant(
        factory,
        label="b",
        order_no="MCP-ORD-B-2002",
        sku="MCP-SKU-B",
        order_total="900.00",
        fx_rate="1.000000",
        gmv_ad_spend="40.00",
    )
    app = create_app()
    app.state.session_factory = factory
    holder: list[CurrentPrincipal] = [
        CurrentPrincipal(
            user_id=tenant_a["user_id"],
            tenant_id=tenant_a["tenant_id"],
            role=UserRole.OWNER,
            email=f"owner@a-{tenant_a['tenant_id'].hex}@mcp-track4.example.com",
        )
    ]
    app.dependency_overrides[get_current_principal] = lambda: holder[0]
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            yield {
                "app": app,
                "client": client,
                "factory": factory,
                "holder": holder,
                "tenant_a": tenant_a,
                "tenant_b": tenant_b,
            }
    finally:
        await _cleanup_tenants(factory, [tenant_a, tenant_b])


async def _issue_token(
    app: FastAPI,
    client: AsyncClient,
    *,
    name: str,
    scopes: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    expires_in_days: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if scopes is not None:
        body["scopes"] = scopes
    if allowed_tools is not None:
        body["allowed_tools"] = allowed_tools
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    response = await client.post(TOKENS_URL, json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _rpc(
    client: AsyncClient,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: Any = 1,
    header: str = "Authorization",
) -> Any:
    response = await client.post(
        MCP_URL,
        headers={header: f"Bearer {token}" if header == "Authorization" else token},
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )
    return response


async def _call_tool(
    client: AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = await _rpc(
        client, token, "tools/call", {"name": name, "arguments": arguments}, request_id=7
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    return body


def _structured(body: dict[str, Any]) -> dict[str, Any]:
    assert "result" in body, body
    result: dict[str, Any] = body["result"]
    assert result["content"][0]["type"] == "text"
    structured: dict[str, Any] = result["structuredContent"]
    return structured


# ---------------------------------------------------------------------------------------
# 1. Token lifecycle, hashing at rest, RBAC and audit trail
# ---------------------------------------------------------------------------------------


async def test_token_lifecycle_stores_only_hash_and_enforces_rbac(
    mcp_env: dict[str, Any],
) -> None:
    client: AsyncClient = mcp_env["client"]
    holder: list[CurrentPrincipal] = mcp_env["holder"]
    factory: Factory = mcp_env["factory"]
    tenant_a: dict[str, UUID] = mcp_env["tenant_a"]

    created = await _issue_token(mcp_env["app"], client, name="Claude Desktop", expires_in_days=30)
    plain_token: str = created["plain_token"]
    meta: dict[str, Any] = created["token"]
    assert plain_token.startswith("erp_mcp_")
    assert meta["token_prefix"] == plain_token[: len(meta["token_prefix"])]
    assert plain_token.startswith(meta["token_prefix"])
    assert meta["scopes"] == ["mcp:read"]
    assert meta["revoked_at"] is None
    assert "token_hash" not in meta and "token_hash" not in created

    # Only the SHA-256 digest + display prefix are ever persisted.
    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT token_prefix, token_hash, scopes, allowed_tools "
                        "FROM mcp_api_tokens WHERE id = :id"
                    ),
                    {"id": UUID(meta["id"])},
                )
            )
            .mappings()
            .one()
        )
        assert row["token_hash"] == hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
        assert row["token_prefix"] == meta["token_prefix"]
        assert plain_token not in row["token_hash"]
        assert row["allowed_tools"] is None
        audit_rows = (
            await session.execute(
                text(
                    "SELECT actor_type, actor_id, action, resource_type "
                    "FROM audit_events WHERE tenant_id = :tid AND action = 'ai.mcp_token.created'"
                ),
                {"tid": tenant_a["tenant_id"]},
            )
        ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].actor_type == "human_user"
    assert audit_rows[0].actor_id == str(tenant_a["user_id"])
    assert audit_rows[0].resource_type == "mcp_api_token"

    # AGENT may not mint tokens; MANAGER may read but not create.
    holder[0] = CurrentPrincipal(
        user_id=tenant_a["user_id"],
        tenant_id=tenant_a["tenant_id"],
        role=UserRole.AGENT,
        email=holder[0].email,
    )
    agent_create = await client.post(TOKENS_URL, json={"name": "agent attempt"})
    assert agent_create.status_code == 403
    assert agent_create.json()["code"] == "ERR_INSUFFICIENT_ROLE"
    assert (await client.get(TOKENS_URL)).status_code == 403

    holder[0] = CurrentPrincipal(
        user_id=tenant_a["user_id"],
        tenant_id=tenant_a["tenant_id"],
        role=UserRole.MANAGER,
        email=holder[0].email,
    )
    assert (await client.get(TOKENS_URL)).status_code == 200
    assert (await client.post(TOKENS_URL, json={"name": "manager attempt"})).status_code == 403

    holder[0] = CurrentPrincipal(
        user_id=tenant_a["user_id"],
        tenant_id=tenant_a["tenant_id"],
        role=UserRole.OWNER,
        email=holder[0].email,
    )
    listed = await client.get(TOKENS_URL)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [meta["id"]]

    revoked = await client.delete(f"{TOKENS_URL}/{meta['id']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    # A revoked token can no longer authenticate.
    rejected = await _rpc(client, plain_token, "initialize")
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "ERR_INVALID_MCP_TOKEN"

    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        revoke_audit = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE tenant_id = :tid AND action = 'ai.mcp_token.revoked'"
            ),
            {"tid": tenant_a["tenant_id"]},
        )
    assert revoke_audit == 1


async def test_token_request_validation_rejects_unknown_scope_and_tool(
    mcp_env: dict[str, Any],
) -> None:
    client: AsyncClient = mcp_env["client"]
    bad_scope = await client.post(TOKENS_URL, json={"name": "bad", "scopes": ["mcp:admin"]})
    assert bad_scope.status_code == 422
    assert "mcp:admin" in bad_scope.json()["detail"]

    bad_tool = await client.post(
        TOKENS_URL, json={"name": "bad", "allowed_tools": ["settle_receivable"]}
    )
    assert bad_tool.status_code == 422
    assert "settle_receivable" in bad_tool.json()["detail"]

    blank_name = await client.post(TOKENS_URL, json={"name": "   "})
    assert blank_name.status_code == 422


# ---------------------------------------------------------------------------------------
# 2. Protocol surface: initialize / tools.list / ping / unknown method / probe / batch
# ---------------------------------------------------------------------------------------


async def test_initialize_tools_list_and_protocol_edges(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    created = await _issue_token(mcp_env["app"], client, name="Cursor", scopes=["mcp:read"])
    token = created["plain_token"]

    init = await _rpc(client, token, "initialize")
    assert init.status_code == 200
    init_body = init.json()
    assert init_body["result"]["protocolVersion"] == "2025-03-26"
    assert init_body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert init_body["result"]["serverInfo"] == {
        "name": "ai-native-erp-mcp",
        "version": "1.0.0",
    }

    ping = await _rpc(client, token, "ping")
    assert ping.json()["result"] == {}

    listed = await _rpc(client, token, "tools/list")
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == _READ_TOOL_NAMES
    for tool in tools:
        assert tool["annotations"]["autonomy_level"] == "L2_auto"
        assert tool["annotations"]["required_scope"] == "mcp:read"
        assert tool["inputSchema"]["type"] == "object"

    unknown_method = await _rpc(client, token, "resources/list")
    assert unknown_method.json()["error"]["code"] == -32601
    assert unknown_method.json()["error"]["message"] == "Method not found"

    unknown_tool = await _rpc(client, token, "tools/call", {"name": "settle_receivable"})
    assert unknown_tool.json()["error"]["code"] == -32602

    # A notification carries no id and therefore gets no JSON-RPC body (202 Accepted).
    notification = await client.post(
        MCP_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notification.status_code == 202
    assert notification.content == b""

    # Batch: two requests in one HTTP call, notification items are silently dropped.
    batch = await client.post(
        MCP_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=[
            {"jsonrpc": "2.0", "id": "a", "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "b", "method": "tools/list"},
        ],
    )
    assert batch.status_code == 200
    batch_body = batch.json()
    assert [item["id"] for item in batch_body] == ["a", "b"]
    assert batch_body[0]["result"] == {}

    # The X-MCP-Token header is an equivalent credential carrier.
    via_header = await _rpc(client, token, "ping", header="X-MCP-Token")
    assert via_header.status_code == 200
    assert via_header.json()["result"] == {}

    # GET probes answer 405 with server identity instead of crashing.
    probe = await client.get(MCP_URL)
    assert probe.status_code == 405
    assert probe.headers["allow"] == "POST"
    assert probe.headers["x-mcp-server"] == "ai-native-erp-mcp/1.0.0"
    assert probe.headers["mcp-protocol-version"] == "2025-03-26"

    # Malformed JSON is a JSON-RPC parse error, not an HTTP 500.
    broken = await client.post(
        MCP_URL,
        headers={"Authorization": f"Bearer {token}"},
        content=b"{not-json",
    )
    assert broken.status_code == 200
    assert broken.json()["error"]["code"] == -32700

    # Garbage and missing credentials are 401.
    for headers in (
        {},
        {"Authorization": "Bearer erp_mcp_not_a_real_token"},
        {"Authorization": "Basic abc"},
        {"X-MCP-Token": "erp_col_wrong_prefix"},
    ):
        unauthorized = await client.post(
            MCP_URL,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert unauthorized.status_code == 401, headers
        assert unauthorized.json()["code"] == "ERR_INVALID_MCP_TOKEN"


# ---------------------------------------------------------------------------------------
# 3. Read tools under real FORCE RLS: Tenant A never sees Tenant B
# ---------------------------------------------------------------------------------------


async def test_read_tools_are_tenant_isolated(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    issued_a = await _issue_token(mcp_env["app"], client, name="Tenant A reader")
    token_a = issued_a["plain_token"]

    sku = _structured(
        await _call_tool(
            client, token_a, "get_live_sku_pricing_and_stock", {"sku": "MCP-SKU-A", "qty": 500}
        )
    )
    assert sku["ok"] is True
    assert sku["data"]["available_qty"] == 1050
    assert sku["data"]["applied_unit_price_usd"] == "14.20"
    assert sku["data"]["source_tool_call_id"] == sku["tool_call_id"]

    other_tenant_sku = _structured(
        await _call_tool(
            client, token_a, "get_live_sku_pricing_and_stock", {"sku": "MCP-SKU-B"}
        )
    )
    assert other_tenant_sku["ok"] is False
    assert other_tenant_sku["error_code"] == "ERR_SKU_NOT_FOUND"

    tracking = _structured(
        await _call_tool(client, token_a, "get_live_order_tracking", {"order_no": "MCP-ORD-A-1001"})
    )
    assert tracking["ok"] is True
    assert tracking["data"]["shipments"][0]["tracking_number"] == "LPA123456CN"

    other_tenant_order = _structured(
        await _call_tool(client, token_a, "get_live_order_tracking", {"order_no": "MCP-ORD-B-2002"})
    )
    assert other_tenant_order["ok"] is False
    assert other_tenant_order["error_code"] == "ERR_ORDER_NOT_FOUND"

    quote = _structured(
        await _call_tool(
            client,
            token_a,
            "calc_b2b_quote_matrix",
            {"items": [{"sku": "MCP-SKU-A", "qty": 500}], "currency": "USD", "incoterm": "FOB"},
        )
    )
    assert quote["ok"] is True
    assert quote["data"]["total_amount"] == "7100.00"
    assert quote["data"]["below_margin_floor"] is False
    assert quote["data"]["items"][0]["sku"] == "MCP-SKU-A"

    receivables = _structured(
        await _call_tool(
            client,
            token_a,
            "match_open_receivables",
            {"amount": "2130.00", "currency": "USD"},
        )
    )
    assert receivables["ok"] is True
    assert len(receivables["data"]["candidates"]) == 1

    analytics = _structured(
        await _call_tool(
            client, token_a, "get_live_channel_sales_analytics", {"window": "30d"}
        )
    )
    assert analytics["ok"] is True
    assert analytics["data"]["totals"]["gmv_base"] == "417.60"
    assert len(analytics["data"]["channels"]) == 1
    assert analytics["data"]["channels"][0]["channel_label"] == "a Shopify"

    sql_rows = _structured(
        await _call_tool(
            client,
            token_a,
            "query_readonly_analytics_sql",
            {
                "sql": (
                    "SELECT order_mode, count(*) AS orders_total "
                    "FROM vw_tenant_order_profit_summary GROUP BY order_mode"
                )
            },
        )
    )
    assert sql_rows["ok"] is True
    assert len(sql_rows["data"]["rows"]) == 1
    assert int(sql_rows["data"]["rows"][0]["orders_total"]) == 1

    # A Tenant B token never sees Tenant A data either: RLS is symmetric.
    mcp_env["holder"][0] = CurrentPrincipal(
        user_id=mcp_env["tenant_b"]["user_id"],
        tenant_id=mcp_env["tenant_b"]["tenant_id"],
        role=UserRole.OWNER,
        email="owner-b@mcp-track4.example.com",
    )
    issued_b = await _issue_token(mcp_env["app"], client, name="Tenant B reader")
    token_b = issued_b["plain_token"]

    # Token management is tenant-scoped too: B cannot list or revoke A's token.
    tokens_b = await client.get(TOKENS_URL)
    assert tokens_b.status_code == 200
    assert [row["id"] for row in tokens_b.json()] == [issued_b["token"]["id"]]
    cross_revoke = await client.delete(f"{TOKENS_URL}/{issued_a['token']['id']}")
    assert cross_revoke.status_code == 404
    assert mcp_env["tenant_a"]["tenant_id"] != mcp_env["tenant_b"]["tenant_id"]

    b_sku = _structured(
        await _call_tool(client, token_b, "get_live_sku_pricing_and_stock", {"sku": "MCP-SKU-B"})
    )
    assert b_sku["ok"] is True
    a_sku_from_b = _structured(
        await _call_tool(client, token_b, "get_live_sku_pricing_and_stock", {"sku": "MCP-SKU-A"})
    )
    assert a_sku_from_b["ok"] is False
    b_analytics = _structured(
        await _call_tool(client, token_b, "get_live_channel_sales_analytics", {"window": "30d"})
    )
    assert b_analytics["data"]["totals"]["gmv_base"] == "900.00"


async def test_tool_call_audit_events_are_ai_agent_scoped(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    factory: Factory = mcp_env["factory"]
    tenant_a = mcp_env["tenant_a"]
    created = await _issue_token(mcp_env["app"], client, name="Auditor")
    token = created["plain_token"]
    token_id = created["token"]["id"]

    await _call_tool(client, token, "get_live_sku_pricing_and_stock", {"sku": "MCP-SKU-A"})
    await _call_tool(client, token, "get_live_order_tracking", {"order_no": "MCP-ORD-A-1001"})

    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT actor_type, actor_id, resource_type, resource_id, after_state "
                    "FROM audit_events WHERE tenant_id = :tid AND action = 'mcp.tool.called' "
                    "ORDER BY created_at"
                ),
                {"tid": tenant_a["tenant_id"]},
            )
        ).all()
    assert len(rows) == 2
    for row in rows:
        assert row.actor_type == "ai_agent"
        assert row.actor_id == f"mcp:{token_id}"
        assert row.resource_type == "mcp_tool"
        assert row.resource_id == UUID(token_id)
        assert row.after_state["ok"] is True
        assert row.after_state["tool_call_id"]
    assert [row.after_state["tool"] for row in rows] == [
        "get_live_sku_pricing_and_stock",
        "get_live_order_tracking",
    ]

    # Tenant B cannot read Tenant A's audit trail.
    mcp_env["holder"][0] = CurrentPrincipal(
        user_id=mcp_env["tenant_b"]["user_id"],
        tenant_id=mcp_env["tenant_b"]["tenant_id"],
        role=UserRole.OWNER,
        email="owner-b@mcp-track4.example.com",
    )
    async with tenant_session(factory, mcp_env["tenant_b"]["tenant_id"]) as session:
        leaked = await session.scalar(
            text("SELECT count(*) FROM audit_events WHERE tenant_id = :tid"),
            {"tid": tenant_a["tenant_id"]},
        )
    assert leaked == 0


# ---------------------------------------------------------------------------------------
# 4. Scope + allowed_tools enforcement
# ---------------------------------------------------------------------------------------


async def test_scope_and_allowed_tools_enforcement(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    read_only = (await _issue_token(mcp_env["app"], client, name="Read only"))["plain_token"]

    denied = await _rpc(
        client,
        read_only,
        "tools/call",
        {
            "name": "create_l1_proposal_draft",
            "arguments": {
                "capability": "sales_quoting",
                "proposal_type": "quote_draft",
                "proposed_payload": {"items": []},
            },
        },
    )
    assert denied.json()["error"]["code"] == -32003
    assert denied.json()["error"]["data"]["required_scope"] == "mcp:propose"

    narrowed = (
        await _issue_token(
            mcp_env["app"],
            client,
            name="Whitelisted",
            scopes=["mcp:read", "mcp:propose"],
            allowed_tools=["get_live_sku_pricing_and_stock"],
        )
    )["plain_token"]
    listed = (await _rpc(client, narrowed, "tools/list")).json()["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["get_live_sku_pricing_and_stock"]

    blocked = await _rpc(
        client,
        narrowed,
        "tools/call",
        {"name": "get_live_order_tracking", "arguments": {"order_no": "MCP-ORD-A-1001"}},
    )
    assert blocked.json()["error"]["code"] == -32003

    # Even with the propose scope, the whitelist keeps the draft tool unreachable.
    draft_blocked = await _rpc(
        client,
        narrowed,
        "tools/call",
        {
            "name": "create_l1_proposal_draft",
            "arguments": {
                "capability": "sales_quoting",
                "proposal_type": "quote_draft",
                "proposed_payload": {"items": []},
            },
        },
    )
    assert draft_blocked.json()["error"]["code"] == -32003

    # Argument validation stays a -32602 and never reaches the tools.
    bad_qty = await _rpc(
        client,
        narrowed,
        "tools/call",
        {"name": "get_live_sku_pricing_and_stock", "arguments": {"sku": "MCP-SKU-A", "qty": 0}},
    )
    assert bad_qty.json()["error"]["code"] == -32602
    float_amount = await _rpc(
        client,
        read_only,
        "tools/call",
        {
            "name": "match_open_receivables",
            "arguments": {"amount": 2130.0, "currency": "USD"},
        },
    )
    assert float_amount.json()["error"]["code"] == -32602


# ---------------------------------------------------------------------------------------
# 5. L1 proposal drafting: pending row, provenance verification, human review
# ---------------------------------------------------------------------------------------


async def test_proposal_draft_is_pending_and_requires_verified_provenance(
    mcp_env: dict[str, Any],
) -> None:
    client: AsyncClient = mcp_env["client"]
    factory: Factory = mcp_env["factory"]
    tenant_a = mcp_env["tenant_a"]
    token = (
        await _issue_token(
            mcp_env["app"], client, name="Proposal writer", scopes=["mcp:read", "mcp:propose"]
        )
    )["plain_token"]

    listed_names = [
        tool["name"] for tool in (await _rpc(client, token, "tools/list")).json()["result"]["tools"]
    ]
    assert listed_names == _ALL_TOOL_NAMES
    draft_descriptor = next(
        tool
        for tool in (await _rpc(client, token, "tools/list")).json()["result"]["tools"]
        if tool["name"] == "create_l1_proposal_draft"
    )
    assert draft_descriptor["annotations"]["autonomy_level"] == "L1_proposal"

    quote = _structured(
        await _call_tool(
            client,
            token,
            "calc_b2b_quote_matrix",
            {"items": [{"sku": "MCP-SKU-A", "qty": 500}], "currency": "USD"},
        )
    )
    source_tool_call_id = quote["tool_call_id"]

    created = _structured(
        await _call_tool(
            client,
            token,
            "create_l1_proposal_draft",
            {
                "capability": "sales_quoting",
                "proposal_type": "quote_draft",
                "proposed_payload": {
                    "currency": "USD",
                    "items": [{"sku": "MCP-SKU-A", "qty": 500}],
                    "total_amount": quote["data"]["total_amount"],
                },
                "citations": [
                    {"kind": "erp_tool", "tool_call_id": source_tool_call_id},
                    {"kind": "note", "text": "MOQ 500 tier applied"},
                ],
                "source_tool_call_id": source_tool_call_id,
                "confidence_score": "0.830",
                "target_entity_type": "order",
                "target_entity_id": str(tenant_a["order_id"]),
            },
        )
    )
    assert created["ok"] is True
    assert created["status"] == "pending"
    assert created["requires_human_review"] is True
    assert created["executed"] is False
    provenance_uuid = str(UUID(hex=source_tool_call_id.removeprefix("tc_")))
    assert created["source_tool_call_id"] == provenance_uuid
    assert created["autonomy_level"] == "L1_proposal"

    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT capability, proposal_type, status, confidence_score, "
                        "source_tool_call_id, target_entity_id, proposed_payload, citations, "
                        "reviewed_by_user_id "
                        "FROM ai_proposals WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_a["tenant_id"], "id": UUID(created["proposal_id"])},
                )
            )
            .mappings()
            .one()
        )
        audit = (
            await session.execute(
                text(
                    "SELECT proposal_id, after_state FROM audit_events "
                    "WHERE tenant_id = :tid AND action = 'mcp.tool.called' "
                    "AND after_state ->> 'tool' = 'create_l1_proposal_draft'"
                ),
                {"tid": tenant_a["tenant_id"]},
            )
        ).all()
    assert row["capability"] == "sales_quoting"
    assert row["proposal_type"] == "quote_draft"
    assert row["status"] == "pending"
    assert str(row["confidence_score"]) == "0.830"
    assert str(row["source_tool_call_id"]) == provenance_uuid
    assert row["target_entity_id"] == tenant_a["order_id"]
    assert row["proposed_payload"]["currency"] == "USD"
    assert row["citations"][0]["tool_call_id"] == source_tool_call_id
    assert row["reviewed_by_user_id"] is None
    assert len(audit) == 1
    assert str(audit[0].proposal_id) == created["proposal_id"]
    assert audit[0].after_state["ok"] is True

    # A fabricated provenance id is rejected and writes no proposal.
    fabricated = await _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "create_l1_proposal_draft",
            "arguments": {
                "capability": "sales_quoting",
                "proposal_type": "quote_draft",
                "proposed_payload": {"items": []},
                "source_tool_call_id": f"tc_{uuid4().hex}",
            },
        },
    )
    assert fabricated.json()["error"]["code"] == -32602
    assert "Provenance check failed" in fabricated.json()["error"]["message"]

    # Capability/proposal_type pairs are validated against the capability matrix.
    mismatched = await _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "create_l1_proposal_draft",
            "arguments": {
                "capability": "biz_analytics",
                "proposal_type": "quote_draft",
                "proposed_payload": {"items": []},
            },
        },
    )
    assert mismatched.json()["error"]["code"] == -32602
    unknown_type = await _rpc(
        client,
        token,
        "tools/call",
        {
            "name": "create_l1_proposal_draft",
            "arguments": {
                "capability": "sales_quoting",
                "proposal_type": "wire_transfer",
                "proposed_payload": {"items": []},
            },
        },
    )
    assert unknown_type.json()["error"]["code"] == -32602

    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        total = await session.scalar(
            text("SELECT count(*) FROM ai_proposals WHERE tenant_id = :tid"),
            {"tid": tenant_a["tenant_id"]},
        )
    assert total == 1

    # The pending proposal is invisible to Tenant B (FORCE RLS fail-closed).
    async with tenant_session(factory, mcp_env["tenant_b"]["tenant_id"]) as session:
        leaked = await session.scalar(
            text(
                "SELECT count(*) FROM ai_proposals WHERE id = :id"
            ),
            {"id": UUID(created["proposal_id"])},
        )
    assert leaked == 0


# ---------------------------------------------------------------------------------------
# 6. Expired tokens are rejected with HTTP 401
# ---------------------------------------------------------------------------------------


async def test_expired_token_is_rejected(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    factory: Factory = mcp_env["factory"]
    tenant_a = mcp_env["tenant_a"]

    raw_expired = "erp_mcp_expired_token_for_track4_test"
    expired_hash = hashlib.sha256(raw_expired.encode("utf-8")).hexdigest()
    async with tenant_session(factory, tenant_a["tenant_id"]) as session:
        await session.execute(
            text(
                "INSERT INTO mcp_api_tokens "
                "(id, tenant_id, created_by_user_id, name, token_prefix, token_hash, "
                "scopes, allowed_tools, expires_at) "
                "VALUES (:id, :tid, :uid, 'Expired', :prefix, :thash, "
                "ARRAY['mcp:read']::TEXT[], NULL, :exp)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_a["tenant_id"],
                "uid": tenant_a["user_id"],
                "prefix": raw_expired[:16],
                "thash": expired_hash,
                "exp": datetime.now(UTC) - timedelta(hours=1),
            },
        )

    rejected = await _rpc(client, raw_expired, "initialize")
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "MCP API token has expired."


# ---------------------------------------------------------------------------------------
# 7. Operator tool catalog: exposed L2/L1 tools, blocked L0 list, client config
# ---------------------------------------------------------------------------------------


async def test_tool_catalog_endpoint_exposes_client_config(mcp_env: dict[str, Any]) -> None:
    client: AsyncClient = mcp_env["client"]
    response = await client.get(CATALOG_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["protocol_version"] == "2025-03-26"
    assert body["server"] == {"name": "ai-native-erp-mcp", "version": "1.0.0"}
    assert [tool["name"] for tool in body["tools"]] == _ALL_TOOL_NAMES
    levels = {tool["name"]: tool["autonomy_level"] for tool in body["tools"]}
    assert levels["create_l1_proposal_draft"] == "L1_proposal"
    assert levels["get_live_sku_pricing_and_stock"] == "L2_auto"
    blocked_names = {tool["name"] for tool in body["blocked_tools"]}
    assert {"settle_receivable", "adjust_inventory", "publish_listing"} <= blocked_names
    server_config = body["config_snippet"]["mcpServers"]["ai-native-erp-mcp"]
    assert server_config["url"].endswith("/api/v1/mcp")
    assert server_config["headers"]["Authorization"].startswith("Bearer erp_mcp_")
