from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.ingestion import (
    DeterministicHashEmbeddingProvider,
    EmbeddingProviderProtocol,
    format_pgvector_literal,
)
from erp_core.shared.db import tenant_session

SEMANTIC_CACHE_THRESHOLD = 0.96
SEMANTIC_CACHE_TTL_SECONDS = 86400
MAX_ENTRIES_PER_TENANT = 200

_DYNAMIC_INTENTS = frozenset(
    {
        "wismo",
        "order_tracking",
        "pricing_stock",
        "b2b_rfq",
        "quote_calc",
        "complex_rfq",
        "finance_recon",
        "payment_match",
        "analytics_query",
    }
)
_DYNAMIC_QUERY_RE = re.compile(
    r"(?:\b(?:ord|pi|trk)[-\s#]?[a-z0-9]{3,}\b|\b(?:prices?|pricing|quote|quotation|moq|stock|"
    r"inventory|orders?|track|tracking|wismo|shipped|shipping|delivery|eta|discount|refund|"
    r"payment|paid|invoice|balance|receivable|availability|lead time)\b|[$€£¥￥]|"
    r"(?:价格|报价|库存|订单|物流|快递|单号|运费|起订量|付款|水单|到账|交期))",
    re.IGNORECASE,
)
_FACT_MARKER = re.compile(r"\b[\w.-]*\d[\w.-]*\b|\b(?:no|not|never|without)\b|不|无|未")

# Timestamp, expiry, and cap are enforced atomically using the Redis server clock.
_PRUNE = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local ttl = tonumber(ARGV[1])
local old = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now - ttl * 1000)
for _, key in ipairs(old) do redis.call('HDEL', KEYS[1], key) end
if #old > 0 then redis.call('ZREM', KEYS[2], unpack(old)) end
"""
_STORE = (
    _PRUNE
    + """
redis.call('HSET', KEYS[1], ARGV[3], ARGV[4])
redis.call('ZADD', KEYS[2], now, ARGV[3])
local excess = redis.call('ZCARD', KEYS[2]) - tonumber(ARGV[2])
if excess > 0 then
    local evict = redis.call('ZRANGE', KEYS[2], 0, excess - 1)
    for _, key in ipairs(evict) do redis.call('HDEL', KEYS[1], key) end
    redis.call('ZREMRANGEBYRANK', KEYS[2], 0, excess - 1)
end
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""
)
_LOOKUP = _PRUNE + "return redis.call('HGETALL', KEYS[1])"


class SemanticCacheLookupResult(BaseModel):
    hit: bool
    bypassed: bool = False
    bypass_reason: str | None = None
    similarity: float = 0.0
    answer_text: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)


class _Entry(BaseModel):
    scope: str
    query: str
    embedding: str
    answer_text: str
    citations: list[dict[str, Any]]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Local reference metric; cache nearest-neighbor scoring runs in PostgreSQL."""
    if len(a) != len(b) or not a:
        return 0.0
    norms = math.hypot(*a) * math.hypot(*b)
    if not norms:
        return 0.0
    return max(-1.0, min(1.0, math.fsum(x * y for x, y in zip(a, b, strict=True)) / norms))


def is_dynamic_query(query: str, intent: str | None = None) -> bool:
    return bool(
        (intent and intent.casefold() in _DYNAMIC_INTENTS) or _DYNAMIC_QUERY_RE.search(query)
    )


def _normalize(query: str) -> str:
    return " ".join(query.casefold().split())


class SemanticCacheService:
    """TTL/cap on Redis; pgvector scores only candidates loaded from this tenant's key."""

    def __init__(
        self,
        redis_client: Redis | None,
        embedding_provider: EmbeddingProviderProtocol | None = None,
        similarity_threshold: float = SEMANTIC_CACHE_THRESHOLD,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if not SEMANTIC_CACHE_THRESHOLD <= similarity_threshold <= 1.0:
            raise ValueError("Semantic cache threshold must be between 0.96 and 1.0.")
        self.redis = redis_client
        self.embedding_provider = embedding_provider or DeterministicHashEmbeddingProvider()
        self.similarity_threshold = similarity_threshold
        self.session_factory = session_factory

    @staticmethod
    def _keys(tenant_id: UUID) -> tuple[str, str]:
        prefix = f"ai:semcache:{tenant_id}:{{{tenant_id}}}:v1"
        return f"{prefix}:entries", f"{prefix}:age"

    async def _vector(self, query: str) -> str:
        (embedding,) = await self.embedding_provider.embed_texts([query])
        return format_pgvector_literal(embedding)

    async def lookup(
        self,
        *,
        tenant_id: UUID,
        query: str,
        intent: str | None = None,
    ) -> SemanticCacheLookupResult:
        return await self._lookup(tenant_id, query, intent, "direct")

    async def _lookup(
        self,
        tenant_id: UUID,
        query: str,
        intent: str | None,
        scope: str,
    ) -> SemanticCacheLookupResult:
        if is_dynamic_query(query, intent):
            return SemanticCacheLookupResult(
                hit=False,
                bypassed=True,
                bypass_reason="dynamic_order_stock_or_quote_intent",
            )
        if self.redis is None or not query.strip():
            return SemanticCacheLookupResult(hit=False)
        try:
            raw = cast(
                list[str],
                await self.redis.eval(
                    _LOOKUP,
                    2,
                    *self._keys(tenant_id),
                    SEMANTIC_CACHE_TTL_SECONDS,
                ),
            )
        except RedisError:
            return SemanticCacheLookupResult(hit=False)
        entries: dict[str, _Entry] = {}
        normalized = _normalize(query)
        markers = _FACT_MARKER.findall(normalized)
        for index in range(0, len(raw), 2):
            try:
                entry = _Entry.model_validate_json(raw[index + 1])
            except ValidationError:
                continue
            if entry.scope != scope or is_dynamic_query(entry.answer_text):
                continue
            if entry.query == normalized:
                return SemanticCacheLookupResult(
                    hit=True,
                    similarity=1.0,
                    answer_text=entry.answer_text,
                    citations=entry.citations,
                )
            if markers == _FACT_MARKER.findall(entry.query):
                entries[raw[index]] = entry
        if not entries or self.session_factory is None:
            return SemanticCacheLookupResult(hit=False)
        candidates = json.dumps(
            [{"id": key, "vec": entry.embedding} for key, entry in entries.items()]
        )
        vector = await self._vector(query)
        async with tenant_session(self.session_factory, tenant_id) as session:
            result = (
                await session.execute(
                    text("""
                WITH scored AS (
                    SELECT id, 1.0 - (CAST(vec AS vector) <=> CAST(CAST(:query AS text) AS vector))
                           AS similarity
                    FROM jsonb_to_recordset(CAST(CAST(:candidates AS text) AS jsonb))
                         AS candidate(id text, vec text)
                )
                SELECT id, similarity FROM scored
                WHERE similarity BETWEEN :threshold AND 1.000001
                ORDER BY similarity DESC, id LIMIT 1
            """),
                    {
                        "query": vector,
                        "candidates": candidates,
                        "threshold": self.similarity_threshold,
                    },
                )
            ).one_or_none()
        if result is None:
            return SemanticCacheLookupResult(hit=False)
        entry = entries[result.id]
        return SemanticCacheLookupResult(
            hit=True,
            similarity=min(1.0, float(result.similarity)),
            answer_text=entry.answer_text,
            citations=entry.citations,
        )

    async def store(
        self,
        *,
        tenant_id: UUID,
        query: str,
        answer_text: str,
        citations: list[dict[str, Any]] | None = None,
        intent: str | None = None,
    ) -> bool:
        return await self._store(tenant_id, query, answer_text, citations, intent, "direct")

    async def _store(
        self,
        tenant_id: UUID,
        query: str,
        answer_text: str,
        citations: list[dict[str, Any]] | None,
        intent: str | None,
        scope: str,
    ) -> bool:
        if (
            self.redis is None
            or not query.strip()
            or len(query) > 2_000
            or not answer_text.strip()
            or len(answer_text) > 10_000
            or is_dynamic_query(query, intent)
            or is_dynamic_query(answer_text)
        ):
            return False
        normalized = _normalize(query)
        entry_id = hashlib.sha256(f"{scope}\0{normalized}".encode()).hexdigest()
        entry = _Entry(
            scope=scope,
            query=normalized,
            embedding=await self._vector(query),
            answer_text=answer_text,
            citations=citations or [],
        ).model_dump_json()
        if len(entry) > 80_000:
            return False
        try:
            await self.redis.eval(
                _STORE,
                2,
                *self._keys(tenant_id),
                SEMANTIC_CACHE_TTL_SECONDS,
                MAX_ENTRIES_PER_TENANT,
                entry_id,
                entry,
            )
        except RedisError:
            return False
        return True
