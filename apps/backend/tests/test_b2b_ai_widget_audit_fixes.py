"""Regression tests for B2B + AI Copilot + Widget + Reconciliation audit fixes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine import runtime as runtime_mod
from erp_core.ai_engine.compliance_filter import ComplianceCheckResult
from erp_core.ai_engine.numeric_guard import ToolGroundedNumericGuard
from erp_core.ai_engine.runtime import (
    PolicyRuntime,
    _sku_pricing_claims,
    _sku_pricing_reply,
)
from erp_core.ai_engine.tools import ToolResult
from erp_core.channels.inbox_service import conversation_unverified_prechat
from erp_core.orders import reconciliation_worker
from erp_core.shared.enums import AiCapability, ChannelType, Incoterm


def _sample_sku_tool(*, ok: bool = True) -> ToolResult:
    if not ok:
        return ToolResult(
            tool_name="get_live_sku_pricing_and_stock",
            ok=False,
            error_code="ERR_SKU_NOT_FOUND",
            error_message="SKU 'UNKNOWN-SKU' not found.",
        )
    tc_id = "tc_live_sku_001"
    return ToolResult(
        tool_name="get_live_sku_pricing_and_stock",
        tool_call_id=tc_id,
        ok=True,
        data={
            "sku": "LED-COB-24V-5M",
            "spu_code": "LED-COB-24V",
            "category": "LED Strip",
            "hs_code": "94054200",
            "certifications": ["CE", "RoHS"],
            "qty_requested": 1,
            "incoterm": "FOB",
            "retail_price_usd": "29.90",
            "applied_moq_tier": 1,
            "applied_unit_price_usd": "29.90",
            "line_total_usd": "29.90",
            "b2b_moq_tiers": [
                {"min_qty": 50, "unit_price_usd": "18.50"},
                {"min_qty": 500, "unit_price_usd": "14.20"},
            ],
            "on_hand_qty": 4500,
            "reserved_qty": 200,
            "available_qty": 4300,
            "lead_time_days": 7,
            "source_tool_call_id": tc_id,
        },
    )


def test_numeric_guard_accepts_grounded_sku_moq_tiers_and_rejects_mismatched_tier() -> None:
    guard = ToolGroundedNumericGuard()
    tool = _sample_sku_tool()

    claims = _sku_pricing_claims(tool)
    assert guard.verify("", [tool], claims).passed is True

    reply_zh = _sku_pricing_reply("查一下 LED-COB-24V-5M 当前可售库存和阶梯价", tool)
    assert "4300" in reply_zh
    assert "MOQ 50+" in reply_zh and "18.50" in reply_zh
    assert "MOQ 500+" in reply_zh and "14.20" in reply_zh
    assert tool.tool_call_id in reply_zh

    reply_en = _sku_pricing_reply("Check live stock and MOQ price for LED-COB-24V-5M", tool)
    assert "Available stock: 4300" in reply_en
    assert "MOQ 500+: USD 14.20/unit" in reply_en

    # Mixing tier 500's price with tier 50's MOQ or inventing a price must still fail.
    bad_tier_mix = {
        "source_tool_call_id": tool.tool_call_id,
        "sku": "LED-COB-24V-5M",
        "incoterm": "FOB",
        "moq": 50,
        "unit_price": "14.20",
    }
    assert guard.verify("", [tool], [bad_tier_mix]).passed is False
    assert (
        guard.verify(
            "",
            [tool],
            [
                {
                    "source_tool_call_id": tool.tool_call_id,
                    "sku": "LED-COB-24V-5M",
                    "incoterm": "FOB",
                    "moq": 500,
                    "unit_price": "9.99",
                }
            ],
        ).passed
        is False
    )
    # Ungrounded free prose without structured claims stays rejected.
    assert guard.verify("LED-COB-24V-5M costs $14.20.", [tool]).passed is False


async def test_policy_runtime_live_sku_tier_query_succeeds_without_abstaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_mod,
        "load_ai_policy",
        AsyncMock(return_value={"cs_support": "L2_auto"}),
    )

    class StubWallet:
        ensure_wallet = AsyncMock(return_value=None)
        lock_budget = AsyncMock(return_value=True)

    class StubCompliance:
        async def load_conversation(self, *_args: Any) -> None:
            return None

        def check_outbound(self, *_args: Any, **_kwargs: Any) -> ComplianceCheckResult:
            return ComplianceCheckResult(allowed=True)

    class StubTools:
        async def get_live_sku_pricing_and_stock(
            self,
            _session: AsyncSession,
            _tenant_id: UUID,
            sku: str,
            qty: int = 1,
            incoterm: Incoterm = Incoterm.FOB,
        ) -> ToolResult:
            del incoterm
            assert sku == "LED-COB-24V-5M"
            assert qty == 1
            return _sample_sku_tool(ok=sku == "LED-COB-24V-5M")

    policy = PolicyRuntime(
        tool_registry=cast(Any, StubTools()),
        wallet_service=cast(Any, StubWallet()),
        compliance_filter=cast(Any, StubCompliance()),
    )
    dummy_session = cast(AsyncSession, object())
    res = await policy.run_capability(
        dummy_session,
        uuid4(),
        AiCapability.CS_SUPPORT,
        {"message": "查一下 LED-COB-24V-5M 当前可售库存和阶梯价"},
        strict_rag=True,
    )
    assert res.should_abstain is False
    assert res.numeric_guard_passed is True
    assert "4300" in res.reply_text
    assert "18.50" in res.reply_text
    assert "14.20" in res.reply_text
    assert res.tool_calls[0]["tool_name"] == "get_live_sku_pricing_and_stock"
    assert res.tool_calls[0]["data"]["available_qty"] == 4300

    class MissingSkuTools:
        async def get_live_sku_pricing_and_stock(
            self, *_args: Any, **_kwargs: Any
        ) -> ToolResult:
            return _sample_sku_tool(ok=False)

    missing_policy = PolicyRuntime(
        tool_registry=cast(Any, MissingSkuTools()),
        wallet_service=cast(Any, StubWallet()),
        compliance_filter=cast(Any, StubCompliance()),
    )
    missing_res = await missing_policy.run_capability(
        dummy_session,
        uuid4(),
        AiCapability.CS_SUPPORT,
        {"message": "查一下 LED-NOT-EXIST-99 价格"},
        strict_rag=True,
    )
    assert missing_res.should_abstain is True
    assert missing_res.reason == "ERR_SKU_NOT_FOUND"
    assert missing_res.reply_text == ""


async def test_reconcile_tenant_orders_isolates_single_account_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    bad_acct_id = uuid4()
    good_acct_id = uuid4()
    wh_id = uuid4()
    rows = [
        SimpleNamespace(id=bad_acct_id, channel_type=ChannelType.SHOPIFY.value, config={}),
        SimpleNamespace(id=good_acct_id, channel_type=ChannelType.WOOCOMMERCE.value, config={}),
    ]

    class FakeSession:
        def __init__(self) -> None:
            self.nested_calls = 0

        async def execute(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: rows)

        @asynccontextmanager
        async def begin_nested(self):
            self.nested_calls += 1
            yield

    fake_session = FakeSession()

    @asynccontextmanager
    async def fake_tenant_session(_factory: Any, _tid: UUID):
        yield fake_session

    async def fake_reconcile_account(
        _session: Any,
        *,
        channel_account_id: UUID,
        **_kwargs: Any,
    ) -> dict[str, int]:
        if channel_account_id == bad_acct_id:
            req = httpx.Request("GET", "https://dead-shop.myshopify.com/admin/api/graphql.json")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)
        return {"recovered": 2, "updated": 1, "failed": 0, "scanned": 3}

    monkeypatch.setattr(reconciliation_worker, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(
        reconciliation_worker, "_adapter_credentials", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "resolve_default_warehouse_id",
        AsyncMock(return_value=wh_id),
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "_instantiate_adapter",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "reconcile_channel_account_orders",
        fake_reconcile_account,
    )

    totals = await reconciliation_worker.reconcile_tenant_orders(cast(Any, object()), tenant_id)
    assert fake_session.nested_calls == 2
    assert totals == {
        "recovered": 2,
        "updated": 1,
        "failed": 1,
        "scanned": 3,
        "accounts": 1,
    }


def test_conversation_unverified_prechat_reads_policy_state_safely() -> None:
    lead = conversation_unverified_prechat(
        {
            "unverified_prechat": {
                "email": " buyer@nordiclight.de ",
                "whatsapp_phone": " +4915123456789 ",
                "marketing_opt_in": True,
            }
        }
    )
    assert lead is not None
    assert lead.email == "buyer@nordiclight.de"
    assert lead.whatsapp_phone == "+4915123456789"
    assert lead.marketing_opt_in is True

    assert conversation_unverified_prechat(None) is None
    assert conversation_unverified_prechat({}) is None
    assert conversation_unverified_prechat({"unverified_prechat": "invalid"}) is None
    assert conversation_unverified_prechat({"unverified_prechat": {"email": "  "}}) is None

    email_only = conversation_unverified_prechat(
        {"unverified_prechat": {"email": "only@example.com"}}
    )
    assert email_only is not None
    assert email_only.email == "only@example.com"
    assert email_only.whatsapp_phone is None
    assert email_only.marketing_opt_in is False
