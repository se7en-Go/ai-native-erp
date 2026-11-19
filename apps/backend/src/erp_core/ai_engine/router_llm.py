from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.semantic_cache import SemanticCacheService, is_dynamic_query
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AiCapability
from erp_core.shared.errors import AppError

CREDIT_COST_USD = Decimal("0.0025")
_ONE_MILLION = Decimal("1000000")
_USD_Q = Decimal("0.000001")
_CREDIT_Q = Decimal("0.0001")


class ModelTier(StrEnum):
    TIER_0_CACHE = "tier_0_cache"
    TIER_1_FAST = "tier_1_fast"
    TIER_2_BALANCED = "tier_2_balanced"
    TIER_3_DEEP = "tier_3_deep"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    provider: str
    tier: ModelTier
    input_per_1m: Decimal
    cached_input_per_1m: Decimal
    output_per_1m: Decimal


MODEL_CATALOG: dict[str, ModelSpec] = {
    "gemini-2.5-flash-lite": ModelSpec(
        model_id="gemini-2.5-flash-lite",
        provider="gemini",
        tier=ModelTier.TIER_1_FAST,
        input_per_1m=Decimal("0.10"),
        cached_input_per_1m=Decimal("0.01"),
        output_per_1m=Decimal("0.40"),
    ),
    "gpt-6-luna": ModelSpec(
        model_id="gpt-6-luna",
        provider="openai",
        tier=ModelTier.TIER_1_FAST,
        input_per_1m=Decimal("0.10"),
        cached_input_per_1m=Decimal("0.01"),
        output_per_1m=Decimal("0.50"),
    ),
    "claude-haiku-5-5": ModelSpec(
        model_id="claude-haiku-5-5",
        provider="anthropic",
        tier=ModelTier.TIER_1_FAST,
        input_per_1m=Decimal("0.10"),
        cached_input_per_1m=Decimal("0.01"),
        output_per_1m=Decimal("0.50"),
    ),
    "gemini-3.1-flash-lite": ModelSpec(
        model_id="gemini-3.1-flash-lite",
        provider="gemini",
        tier=ModelTier.TIER_2_BALANCED,
        input_per_1m=Decimal("0.25"),
        cached_input_per_1m=Decimal("0.025"),
        output_per_1m=Decimal("1.50"),
    ),
    "deepseek-flash": ModelSpec(
        model_id="deepseek-flash",
        provider="deepseek",
        tier=ModelTier.TIER_2_BALANCED,
        input_per_1m=Decimal("0.30"),
        cached_input_per_1m=Decimal("0.006"),
        output_per_1m=Decimal("1.20"),
    ),
    "gpt-5-mini": ModelSpec(
        model_id="gpt-5-mini",
        provider="openai",
        tier=ModelTier.TIER_2_BALANCED,
        input_per_1m=Decimal("0.25"),
        cached_input_per_1m=Decimal("0.025"),
        output_per_1m=Decimal("2.00"),
    ),
    "claude-sonnet-5-5": ModelSpec(
        model_id="claude-sonnet-5-5",
        provider="anthropic",
        tier=ModelTier.TIER_3_DEEP,
        input_per_1m=Decimal("2.00"),
        cached_input_per_1m=Decimal("0.10"),
        output_per_1m=Decimal("10.00"),
    ),
    "gpt-6.2-sol": ModelSpec(
        model_id="gpt-6.2-sol",
        provider="openai",
        tier=ModelTier.TIER_3_DEEP,
        input_per_1m=Decimal("2.00"),
        cached_input_per_1m=Decimal("0.10"),
        output_per_1m=Decimal("10.00"),
    ),
    "deepseek-v4-pro": ModelSpec(
        model_id="deepseek-v4-pro",
        provider="deepseek",
        tier=ModelTier.TIER_3_DEEP,
        input_per_1m=Decimal("1.32"),
        cached_input_per_1m=Decimal("0.044"),
        output_per_1m=Decimal("3.96"),
    ),
}

_DEFAULT_MODEL_BY_TIER: dict[ModelTier, list[str]] = {
    ModelTier.TIER_1_FAST: ["gemini-2.5-flash-lite", "gpt-6-luna", "claude-haiku-5-5"],
    ModelTier.TIER_2_BALANCED: ["gemini-3.1-flash-lite", "deepseek-flash", "gpt-5-mini"],
    ModelTier.TIER_3_DEEP: ["claude-sonnet-5-5", "gpt-6.2-sol", "deepseek-v4-pro"],
}


def calculate_cost_and_credits(
    model_id: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> tuple[Decimal, Decimal]:
    spec = MODEL_CATALOG[model_id]
    safe_cached = max(0, min(prompt_tokens, cached_tokens))
    uncached = max(0, prompt_tokens - safe_cached)
    raw_cost = (
        Decimal(uncached) * spec.input_per_1m
        + Decimal(safe_cached) * spec.cached_input_per_1m
        + Decimal(max(0, completion_tokens)) * spec.output_per_1m
    ) / _ONE_MILLION
    cost_usd = raw_cost.quantize(_USD_Q, rounding=ROUND_HALF_UP)
    credits = (cost_usd / CREDIT_COST_USD).quantize(_CREDIT_Q, rounding=ROUND_HALF_UP)
    return cost_usd, credits


class LlmProviderResponse(BaseModel):
    text: str
    prompt_tokens: int
    cached_tokens: int = 0
    completion_tokens: int
    model_id: str
    provider: str


class LlmProviderClientProtocol(Protocol):
    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        """Execute a chat completion call against the configured provider."""


class AsyncMultiProviderClient:
    """Unified async HTTP client for OpenAI, Anthropic, Gemini, and DeepSeek with prompt caching."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        crypto_service: EnvelopeCryptoService | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.openai_base_url = settings.OPENAI_BASE_URL.rstrip("/")
        self.keys = {
            "openai": settings.OPENAI_API_KEY,
            "anthropic": settings.ANTHROPIC_API_KEY,
            "gemini": settings.GEMINI_API_KEY,
            "deepseek": settings.DEEPSEEK_API_KEY,
        }
        self.session_factory = session_factory
        self.crypto_service = crypto_service or EnvelopeCryptoService(settings)
        self.transport = transport

    def has_key(self, provider: str) -> bool:
        return bool(self.keys.get(provider))

    async def tenant_envelopes(self, tenant_id: UUID) -> dict[str, Any]:
        if self.session_factory is None:
            return {}
        async with tenant_session(self.session_factory, tenant_id) as session:
            stored = await session.scalar(
                text("""
                SELECT ai_policy_settings -> 'llm_byok' FROM tenants
                WHERE id = :tenant AND is_active AND shredded_at IS NULL
            """),
                {"tenant": tenant_id},
            )
        if stored is None:
            return {}
        if not isinstance(stored, dict):
            raise RuntimeError("Tenant provider credential unavailable")
        return {**stored}

    async def complete_for_tenant(
        self,
        *,
        tenant_id: UUID,
        envelope: Any,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        if self.session_factory is None or not isinstance(envelope, dict):
            raise RuntimeError("Tenant provider credential unavailable")
        try:
            async with tenant_session(self.session_factory, tenant_id) as session:
                api_key = await self.crypto_service.decrypt_secret(
                    session,
                    tenant_id,
                    UUID(envelope["account_id"]),
                    f"llm_byok:{provider}",
                    bytes.fromhex(envelope["ciphertext"]),
                    bytes.fromhex(envelope["nonce"]),
                    bytes.fromhex(envelope["auth_tag"]),
                    int(envelope["key_version"]),
                )
        except (AppError, ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError("Tenant provider credential unavailable") from None
        if not api_key or not api_key.isascii() or any(ch.isspace() for ch in api_key):
            raise RuntimeError("Tenant provider credential unavailable")
        return await self._request(api_key, model_id, provider, messages, max_tokens, temperature)

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        api_key = self.keys.get(provider)
        if not api_key:
            raise RuntimeError(f"Provider '{provider}' API key is not configured.")
        return await self._request(
            api_key, model_id, provider, messages, max_tokens, temperature,
            openai_base_url=self.openai_base_url,
        )

    async def _request(
        self,
        api_key: str,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        *,
        openai_base_url: str = "https://api.openai.com/v1",
    ) -> LlmProviderResponse:
        # Provider exceptions may include requests, URLs or response material. Never expose them.
        try:
            response = await self._send(
                api_key,
                model_id,
                provider,
                messages,
                max_tokens,
                temperature,
                openai_base_url=openai_base_url,
            )
        except (httpx.HTTPError, TypeError, ValueError, IndexError, KeyError, AttributeError):
            raise RuntimeError("LLM provider request failed") from None
        if api_key in response.text:
            raise RuntimeError("LLM provider response contained a credential")
        return response

    async def _send(
        self,
        api_key: str,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        *,
        openai_base_url: str,
    ) -> LlmProviderResponse:
        async with httpx.AsyncClient(
            timeout=45.0,
            transport=self.transport,
            trust_env=False,
        ) as client:
            if provider in {"openai", "deepseek"}:
                base_url = (
                    f"{openai_base_url}/chat/completions"
                    if provider == "openai"
                    else "https://api.deepseek.com/v1/chat/completions"
                )
                clean_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
                resp = await client.post(
                    base_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model_id,
                        "messages": clean_msgs,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage", {})
                cached = int(
                    usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                    or usage.get("prompt_cache_hit_tokens", 0)
                )
                return LlmProviderResponse(
                    text=data["choices"][0]["message"]["content"],
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    cached_tokens=cached,
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    model_id=model_id,
                    provider=provider,
                )

            if provider == "anthropic":
                system_blocks = [
                    {
                        "type": "text",
                        "text": m["content"],
                        **({"cache_control": m["cache_control"]} if "cache_control" in m else {}),
                    }
                    for m in messages
                    if m["role"] == "system"
                ]
                user_msgs = [
                    {"role": m["role"], "content": m["content"]}
                    for m in messages
                    if m["role"] != "system"
                ]
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": model_id,
                        "system": system_blocks,
                        "messages": user_msgs,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage", {})
                text_out = "".join(
                    block.get("text", "")
                    for block in data.get("content", [])
                    if block.get("type") == "text"
                )
                return LlmProviderResponse(
                    text=text_out,
                    prompt_tokens=int(usage.get("input_tokens", 0))
                    + int(usage.get("cache_read_input_tokens", 0))
                    + int(usage.get("cache_creation_input_tokens", 0)),
                    cached_tokens=int(usage.get("cache_read_input_tokens", 0)),
                    completion_tokens=int(usage.get("output_tokens", 0)),
                    model_id=model_id,
                    provider=provider,
                )

            if provider == "gemini":
                system_text = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
                contents = [
                    {
                        "role": "user" if m["role"] == "user" else "model",
                        "parts": [{"text": m["content"]}],
                    }
                    for m in messages
                    if m["role"] != "system"
                ]
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json={
                        "systemInstruction": {"parts": [{"text": system_text}]},
                        "contents": contents,
                        "generationConfig": {
                            "maxOutputTokens": max_tokens,
                            "temperature": temperature,
                        },
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usageMetadata", {})
                text_out = data["candidates"][0]["content"]["parts"][0]["text"]
                return LlmProviderResponse(
                    text=text_out,
                    prompt_tokens=int(usage.get("promptTokenCount", 0)),
                    cached_tokens=int(usage.get("cachedContentTokenCount", 0)),
                    completion_tokens=int(usage.get("candidatesTokenCount", 0)),
                    model_id=model_id,
                    provider=provider,
                )

        raise ValueError(f"Unsupported LLM provider: {provider}")


class DeterministicOfflineLlmClient:
    """Offline structured responder used when executing deterministic evals or unit tests."""

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        context_text = "\n".join(str(m.get("content", "")) for m in messages)
        return LlmProviderResponse(
            text=json.dumps({"grounded_context_len": len(context_text), "status": "ok"}),
            prompt_tokens=max(200, len(context_text) // 4),
            cached_tokens=max(100, len(messages[0].get("content", "")) // 4) if messages else 0,
            completion_tokens=90,
            model_id=model_id,
            provider=provider,
        )


def build_prompt_cache_messages(
    *,
    system_prefix: str,
    tool_definitions: list[dict[str, Any]] | None,
    context_block: str,
    user_query: str,
) -> list[dict[str, Any]]:
    """Place invariant system instructions and tool schemas in the cached prefix."""
    static_prefix = system_prefix.strip()
    if tool_definitions:
        static_prefix += "\n\nTool Schemas:\n" + json.dumps(tool_definitions, sort_keys=True)
    user_body = (
        f"Context:\n{context_block.strip()}\n\nUser Input:\n{user_query.strip()}"
        if context_block.strip()
        else user_query.strip()
    )
    return [
        {
            "role": "system",
            "content": static_prefix,
            "cache_control": {"type": "ephemeral"},
        },
        {"role": "user", "content": user_body},
    ]


class LlmGenerationResult(BaseModel):
    text: str
    tier: ModelTier
    model_id: str
    provider: str
    cache_hit: bool = False
    cache_bypass_reason: str | None = None
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal("0.000000")
    credits_consumed: Decimal = Decimal("0.0000")
    citations: list[dict[str, Any]] = Field(default_factory=list)


class TieredModelRouter:
    """Select a cached response or a model tier and account for its Decimal cost."""

    def __init__(
        self,
        semantic_cache: SemanticCacheService | None = None,
        provider_client: LlmProviderClientProtocol | None = None,
    ) -> None:
        self.semantic_cache = semantic_cache
        self.provider_client = provider_client or AsyncMultiProviderClient()

    def select_tier(
        self,
        *,
        capability: AiCapability,
        intent: str | None = None,
        sku_count: int = 0,
        has_attachments: bool = False,
        text_length: int = 0,
        explicit_tier: ModelTier | None = None,
    ) -> ModelTier:
        if explicit_tier is not None and explicit_tier != ModelTier.TIER_0_CACHE:
            return explicit_tier
        if intent in {"triage", "classify", "translate", "compliance_check"}:
            return ModelTier.TIER_1_FAST
        if (
            capability == AiCapability.SALES_QUOTING
            and (sku_count >= 2 or has_attachments or text_length > 1200)
        ) or intent in {"complex_rfq", "negotiation_dispute", "multi_sku_bom"}:
            return ModelTier.TIER_3_DEEP
        return ModelTier.TIER_2_BALANCED

    def _pick_model(self, tier: ModelTier, tenant_envelopes: dict[str, Any]) -> ModelSpec:
        candidates = _DEFAULT_MODEL_BY_TIER[tier]
        if isinstance(self.provider_client, AsyncMultiProviderClient):
            for cid in candidates:
                spec = MODEL_CATALOG[cid]
                if spec.provider in tenant_envelopes:
                    return spec
            for cid in candidates:
                spec = MODEL_CATALOG[cid]
                if self.provider_client.has_key(spec.provider):
                    return spec
        return MODEL_CATALOG[candidates[0]]

    async def generate(
        self,
        *,
        tenant_id: UUID,
        capability: AiCapability,
        system_prefix: str,
        context_block: str,
        user_query: str,
        intent: str | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        sku_count: int = 0,
        has_attachments: bool = False,
        explicit_tier: ModelTier | None = None,
        cacheable_faq: bool = False,
        max_tokens: int = 600,
        temperature: float = 0.1,
    ) -> LlmGenerationResult:
        cache_bypass_reason: str | None = None
        eligible = (
            self.semantic_cache is not None
            and cacheable_faq
            and capability == AiCapability.CS_SUPPORT
            and intent in {None, "faq_static"}
            and not has_attachments
            and not tool_definitions
            and sku_count <= 1
            and explicit_tier in {None, ModelTier.TIER_0_CACHE}
        )
        if cacheable_faq and is_dynamic_query(user_query, intent):
            cache_bypass_reason = "dynamic_order_stock_or_quote_intent"
            eligible = False
        scope = hashlib.sha256(
            json.dumps(
                [capability.value, intent, system_prefix, context_block],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if self.semantic_cache is not None and eligible:
            lookup = await self.semantic_cache._lookup(tenant_id, user_query, intent, scope)
            if lookup.hit and lookup.answer_text is not None:
                return LlmGenerationResult(
                    text=lookup.answer_text,
                    tier=ModelTier.TIER_0_CACHE,
                    model_id="semantic_cache_v1",
                    provider="redis",
                    cache_hit=True,
                    citations=lookup.citations,
                )
            cache_bypass_reason = lookup.bypass_reason

        tier = self.select_tier(
            capability=capability,
            intent=intent,
            sku_count=sku_count,
            has_attachments=has_attachments,
            text_length=len(user_query),
            explicit_tier=explicit_tier,
        )
        client = self.provider_client
        envelopes = (
            await client.tenant_envelopes(tenant_id)
            if isinstance(client, AsyncMultiProviderClient)
            else {}
        )
        spec = self._pick_model(tier, envelopes)
        uses_byok = spec.provider in envelopes
        messages = build_prompt_cache_messages(
            system_prefix=system_prefix,
            tool_definitions=tool_definitions,
            context_block=context_block,
            user_query=user_query,
        )

        if uses_byok and isinstance(client, AsyncMultiProviderClient):
            resp = await client.complete_for_tenant(
                tenant_id=tenant_id,
                envelope=envelopes[spec.provider],
                model_id=spec.model_id,
                provider=spec.provider,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        else:
            resp = await client.complete(
                model_id=spec.model_id,
                provider=spec.provider,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        cost_usd, credits = calculate_cost_and_credits(
            spec.model_id,
            resp.prompt_tokens,
            resp.cached_tokens,
            resp.completion_tokens,
        )

        if self.semantic_cache is not None and eligible and citations and not uses_byok:
            await self.semantic_cache._store(
                tenant_id,
                user_query,
                resp.text,
                citations,
                intent,
                scope,
            )

        return LlmGenerationResult(
            text=resp.text,
            tier=tier,
            model_id=spec.model_id,
            provider=spec.provider,
            cache_hit=False,
            cache_bypass_reason=cache_bypass_reason,
            prompt_tokens=resp.prompt_tokens,
            cached_tokens=resp.cached_tokens,
            completion_tokens=resp.completion_tokens,
            cost_usd=cost_usd,
            credits_consumed=credits,
            citations=citations or [],
        )
