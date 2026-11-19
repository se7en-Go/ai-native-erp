from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.ingestion import DeterministicHashEmbeddingProvider
from erp_core.ai_engine.router_llm import (
    CREDIT_COST_USD,
    MODEL_CATALOG,
    AsyncMultiProviderClient,
    LlmProviderResponse,
    ModelTier,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.ai_engine.semantic_cache import SemanticCacheService
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AiCapability
from erp_core.shared.redis_client import create_redis_client


@pytest.fixture
async def redis_tenants() -> AsyncIterator[tuple[UUID, UUID]]:
    tenants = uuid4(), uuid4()
    redis = create_redis_client(get_settings())
    try:
        yield tenants
    finally:
        for tenant in tenants:
            keys = [key async for key in redis.scan_iter(match=f"ai:semcache:{tenant}:*")]
            if keys:
                await redis.delete(*keys)
        await redis.aclose()


class SimilarParaphraseEmbedder(DeterministicHashEmbeddingProvider):
    def embed_one(self, text_value: str) -> list[float]:
        if text_value.startswith("Warranty duration"):
            return super().embed_one("What is the warranty period for XJ-400Pro-EU?")
        return super().embed_one(text_value)


class CountingMockLlmClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        self.calls.append({"model_id": model_id, "provider": provider, "messages": messages})
        return LlmProviderResponse(
            text="Our XJ-400Pro-EU floodlight carries a 5-year warranty [doc#chunk].",
            prompt_tokens=2000,
            cached_tokens=1500,
            completion_tokens=100,
            model_id=model_id,
            provider=provider,
        )


@pytest.mark.asyncio
async def test_decimal_cost_to_credit_mapping() -> None:
    # gemini-3.1-flash-lite: input $0.25/1M, cached $0.025/1M, output $1.50/1M
    # uncached = 2000 - 1500 = 500 tokens => 500 * 0.25 = 125
    # cached = 1500 * 0.025 = 37.5
    # output = 100 * 1.50 = 150
    # total = 312.5 / 1,000,000 = $0.000313 (quantized 6dp: 0.0003125 -> 0.000313 ROUND_HALF_UP)
    cost_usd, credits = calculate_cost_and_credits(
        model_id="gemini-3.1-flash-lite",
        prompt_tokens=2000,
        cached_tokens=1500,
        completion_tokens=100,
    )
    assert Decimal("0.0025") == CREDIT_COST_USD
    assert cost_usd == Decimal("0.000313")
    assert credits == (Decimal("0.000313") / Decimal("0.0025")).quantize(Decimal("0.0001"))

    # Exact multiple check: 1,000,000 output tokens on gpt-6.2-sol ($10.00) = 4000.0000 credits
    sol_cost, sol_credits = calculate_cost_and_credits(
        model_id="gpt-6.2-sol",
        prompt_tokens=0,
        cached_tokens=0,
        completion_tokens=1_000_000,
    )
    assert sol_cost == Decimal("10.000000")
    assert sol_credits == Decimal("4000.0000")


@pytest.mark.asyncio
async def test_tier0_semantic_cache_hit_skips_llm_and_bypasses_dynamic_queries(
    runtime_session_factory: async_sessionmaker[AsyncSession],
    redis_tenants: tuple[UUID, UUID],
) -> None:
    tenant_a, tenant_b = redis_tenants
    redis_client = create_redis_client(get_settings())
    try:
        await exercise_router(runtime_session_factory, tenant_a, tenant_b, redis_client)
    finally:
        await redis_client.aclose()


async def exercise_router(
    sessions: async_sessionmaker[AsyncSession],
    tenant_a: UUID,
    tenant_b: UUID,
    redis_client: Redis,
) -> None:
    embedder = SimilarParaphraseEmbedder()
    cache = SemanticCacheService(
        redis_client=redis_client,
        embedding_provider=embedder,
        session_factory=sessions,
    )
    mock_client = CountingMockLlmClient()
    router = TieredModelRouter(semantic_cache=cache, provider_client=mock_client)

    # 1. Seed FAQ answer via Tier 2 LLM call (Miss -> LLM -> Store)
    res1 = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.CS_SUPPORT,
        system_prefix="You are a strict LED factory support assistant.",
        context_block="Chunk [1111#2222]: XJ-400Pro-EU includes a 5-year warranty.",
        user_query="What is the warranty period for XJ-400Pro-EU?",
        intent="faq_static",
        citations=[{"doc_id": "1111", "chunk_id": "2222"}],
        cacheable_faq=True,
    )
    assert res1.tier == ModelTier.TIER_2_BALANCED
    assert len(mock_client.calls) == 1
    assert res1.cost_usd > Decimal("0")

    # 2. Same/paraphrased static FAQ query for Tenant A hits Tier 0 cache ($0, 0 LLM calls)
    res2 = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.CS_SUPPORT,
        system_prefix="You are a strict LED factory support assistant.",
        context_block="Chunk [1111#2222]: XJ-400Pro-EU includes a 5-year warranty.",
        user_query="Warranty duration for XJ-400Pro-EU?",
        intent="faq_static",
        cacheable_faq=True,
    )
    assert res2.tier == ModelTier.TIER_0_CACHE
    assert res2.cache_hit is True
    assert res2.cost_usd == Decimal("0.000000")
    assert res2.credits_consumed == Decimal("0.0000")
    assert len(mock_client.calls) == 1  # No additional LLM call

    # 3. Tenant B isolation: Tenant B NEVER sees Tenant A's cached answer
    res_b = await router.generate(
        tenant_id=tenant_b,
        capability=AiCapability.CS_SUPPORT,
        system_prefix="You are a strict LED factory support assistant.",
        context_block="Chunk [1111#2222]: XJ-400Pro-EU includes a 5-year warranty.",
        user_query="What is the warranty period for XJ-400Pro-EU?",
        intent="faq_static",
        cacheable_faq=True,
    )
    assert res_b.cache_hit is False
    assert len(mock_client.calls) == 2

    # 4. Dynamic order/stock/quote query ALWAYS bypasses Tier 0 semantic cache
    res_dyn = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.ORDER_LOGISTICS,
        system_prefix="You are an order logistics assistant.",
        context_block="Live order tracking ORD-2026-8801",
        user_query="Where is my order ORD-2026-8801 and what is the live stock price?",
        intent="wismo",
        cacheable_faq=True,
    )
    assert res_dyn.cache_hit is False
    assert res_dyn.cache_bypass_reason == "dynamic_order_stock_or_quote_intent"
    assert len(mock_client.calls) == 3

    # 5. Multi-SKU B2B RFQ escalates to Tier 3 frontier model
    res_t3 = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.SALES_QUOTING,
        system_prefix="You are a B2B sales engineer.",
        context_block="Multi-SKU RFQ matrix",
        user_query="Please prepare an FOB quote for 500 XJ-400Pro-EU and 300 XJ-200Slim-US.",
        intent="b2b_rfq",
        sku_count=2,
        has_attachments=True,
    )
    assert res_t3.tier == ModelTier.TIER_3_DEEP
    assert MODEL_CATALOG[res_t3.model_id].tier == ModelTier.TIER_3_DEEP
    # Verify prompt-cache prefix ordering places system_prefix before dynamic user query
    sent_messages = mock_client.calls[-1]["messages"]
    assert sent_messages[0]["role"] == "system"
    assert sent_messages[0].get("cache_control") == {"type": "ephemeral"}

    fast = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.CS_SUPPORT,
        system_prefix="Translate only",
        context_block="",
        user_query="Hello",
        intent="translate",
    )
    assert fast.tier == ModelTier.TIER_1_FAST
    mismatch = await router.generate(
        tenant_id=tenant_a,
        capability=AiCapability.CS_SUPPORT,
        system_prefix="Different policy",
        context_block="Warranty prevented by policy",
        user_query="What is the warranty period for XJ-400Pro-EU?",
        intent="faq_static",
        cacheable_faq=True,
    )
    assert not mismatch.cache_hit
    keys = [key async for key in redis_client.scan_iter(match=f"ai:semcache:{tenant_a}:*")]
    assert keys
    for key in keys:
        assert await redis_client.ttl(key) > 0


async def test_router_envelope_byok_is_tenant_bound_and_only_sent_to_provider(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = get_settings()
    crypto = EnvelopeCryptoService(settings)
    tenant_a, tenant_b, account_id = uuid4(), uuid4(), uuid4()
    secret = "example-byok-credential-for-unit-test"
    observed: list[str | None] = []

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        assert secret not in request.url.query.decode() + request.content.decode()
        observed.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Public FAQ answer"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
            },
        )

    async with tenant_session(runtime_session_factory, tenant_a) as db:
        await db.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'BYOK A')"),
            {"id": tenant_a, "slug": str(tenant_a)},
        )
        enc, nonce, tag, version = await crypto.encrypt_secret(
            db,
            tenant_a,
            account_id,
            "llm_byok:openai",
            secret,
        )
        envelope = {
            "openai": {
                "account_id": str(account_id),
                "ciphertext": enc.hex(),
                "nonce": nonce.hex(),
                "auth_tag": tag.hex(),
                "key_version": version,
            }
        }
        stored = json.dumps({"llm_byok": envelope})
        assert secret not in stored
        await db.execute(
            text("UPDATE tenants SET ai_policy_settings = CAST(:value AS jsonb) WHERE id = :id"),
            {"id": tenant_a, "value": stored},
        )
    async with tenant_session(runtime_session_factory, tenant_b) as db:
        await db.execute(
            text(
                "INSERT INTO tenants (id, slug, name, ai_policy_settings) "
                "VALUES (:id, :slug, 'BYOK B', CAST(:value AS jsonb))"
            ),
            {"id": tenant_b, "slug": str(tenant_b), "value": stored},
        )
    provider = AsyncMultiProviderClient(
        session_factory=runtime_session_factory,
        crypto_service=crypto,
        transport=httpx.MockTransport(receive),
    )
    provider.keys.clear()
    router = TieredModelRouter(provider_client=provider)
    try:
        result = await router.generate(
            tenant_id=tenant_a,
            capability=AiCapability.CS_SUPPORT,
            system_prefix="Public FAQ",
            context_block="",
            user_query="Hello",
        )
        assert result.provider == "openai"
        assert secret not in result.model_dump_json()
        assert observed == [f"Bearer {secret}"]
        with pytest.raises(RuntimeError, match="Tenant provider credential unavailable"):
            await router.generate(
                tenant_id=tenant_b,
                capability=AiCapability.CS_SUPPORT,
                system_prefix="Public FAQ",
                context_block="",
                user_query="Hello",
            )
        assert len(observed) == 1
    finally:
        # Only these freshly seeded tenants, with no audit or business rows.
        for tenant in (tenant_a, tenant_b):
            async with tenant_session(runtime_session_factory, tenant) as db:
                await db.execute(
                    text("DELETE FROM tenant_keys WHERE tenant_id = :id"), {"id": tenant}
                )
                await db.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant})
