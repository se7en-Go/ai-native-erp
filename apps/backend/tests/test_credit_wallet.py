from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

from erp_core.billing import wallet_service
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AutonomyLevel, UsageMetricType


async def test_atomic_reservations_and_idempotent_fractional_accounting(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    service = wallet_service.CreditWalletService()
    async with tenant_session(runtime_session_factory, tid) as session:
        await service.ensure_wallet(session, tenant_id=tid, included_credits=3)

    async def reserve(key: str, units: str) -> tuple[bool, AutonomyLevel | None]:
        async with tenant_session(runtime_session_factory, tid) as session:
            return await service.try_reserve_and_record_spend(
                session,
                tid,
                key,
                UsageMetricType.AI_CREDIT,
                Decimal(units),
                Decimal("0.0123455"),
                "test-meter",
            )

    responses = await asyncio.gather(*(reserve(f"call-{i}", "1.50004") for i in range(8)))
    assert responses.count((True, None)) == 2
    assert responses.count((False, AutonomyLevel.L0_COPILOT)) == 6
    key = f"call-{responses.index((True, None))}"
    assert await reserve(key, "1.50004") == (True, None)
    async with tenant_session(runtime_session_factory, tid) as session:
        wallet = await service.get_wallet(session, tid)
        assert wallet.total_credits_available == 0
        assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 2
        assert await session.scalar(
            text("SELECT sum(raw_provider_cost_usd) FROM usage_ledger")
        ) == Decimal("0.024692")


async def test_stripe_signature_replay_and_l0_downgrade(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    headers = {"Authorization": f"Bearer {token}"}
    secret = "whsec_local_test"
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", secret)
    service = wallet_service.CreditWalletService()
    async with tenant_session(runtime_session_factory, tid) as session:
        await service.ensure_wallet(session, tenant_id=tid, included_credits=0)
    chat = await async_client.post(
        "/api/v1/ai/assistant/chat",
        headers=headers,
        json={
            "capability": "content_studio",
            "message": "Summarize the conversation",
        },
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["degraded_to_l0"] and not chat.json()["auto_executed"]
    assert chat.json()["model_used"] == ""

    payment = f"pi_{uuid4().hex}"
    obj = {
        "id": payment,
        "status": "succeeded",
        "currency": "usd",
        "amount_received": 100,
        "metadata": {"tenant_id": raw_tid, "erp_credit_topup": "1", "credits": "100"},
    }
    url = "/api/v1/billing/stripe/webhook"
    raw = json.dumps(
        {"id": "evt_one", "type": "payment_intent.succeeded", "data": {"object": obj}}
    ).encode()
    invalid = await async_client.post(url, content=raw, headers={"Stripe-Signature": "bad"})
    assert invalid.status_code == 401
    for event_id in ("evt_one", "evt_one", "evt_two"):
        raw = json.dumps(
            {"id": event_id, "type": "payment_intent.succeeded", "data": {"object": obj}}
        ).encode()
        stamp = int(time.time())
        sig = hmac.new(
            secret.encode(), str(stamp).encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        received = await async_client.post(
            url, content=raw, headers={"Stripe-Signature": f"t={stamp},v1={sig}"}
        )
        assert received.status_code == 200, received.text
    wallet = await async_client.get("/api/v1/billing/wallet", headers=headers)
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["topup_credits_balance"] == 100
    meter = await async_client.get("/api/v1/billing/usage", headers=headers)
    assert meter.status_code == 200 and meter.json()["total"] == 0
