from __future__ import annotations

import re
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.ingestion import (
    EmbeddingProviderProtocol,
    OpenAIEmbeddingProvider,
    format_pgvector_literal,
    strip_dynamic_prices_for_answer,
)
from erp_core.shared.enums import ConversationStatus

ABSTENTION_THRESHOLD = 0.62

_STOPWORDS = frozenset(
    {
        "what",
        "is",
        "are",
        "was",
        "were",
        "the",
        "of",
        "for",
        "a",
        "an",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "does",
        "do",
        "did",
        "can",
        "could",
        "would",
        "should",
        "you",
        "your",
        "we",
        "our",
        "me",
        "my",
        "please",
        "tell",
        "about",
        "how",
        "much",
        "many",
        "this",
        "that",
        "these",
        "those",
        "have",
        "has",
        "had",
        "be",
        "from",
        "it",
    }
)
_SKU_OR_CODE_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+")
_ALNUM_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}|[一-龯]")
_ALPHA_DIGIT_SPLIT_RE = re.compile(r"[A-Za-z]+|\d+")
_INLINE_CITATION_RE = re.compile(
    r"\[([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})#"
    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\]"
)
_SENTENCE_SPLIT_RE = re.compile(r"[!?。！？\n]+|(?<![0-9])\.|\.(?![0-9])")
_MALFORMED_CITATION_RE = re.compile(r"\[[^\]\n]*#[^\]\n]*\]")


class RagCandidate(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    chunk_index: int
    raw_content: str
    has_dynamic_price_disclaimer: bool
    rrf_score: float
    bm25_score: float
    cosine_sim: float


class RagHit(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    doc_title: str
    chunk_index: int
    content: str
    has_dynamic_price_disclaimer: bool
    rrf_score: float
    bm25_score: float
    cosine_sim: float
    rerank_score: float


class RagSearchResult(BaseModel):
    query: str
    strict_mode: bool
    hits: list[RagHit] = Field(default_factory=list)
    top_score: float = 0.0
    should_abstain: bool = True
    abstention_reason: str | None = None
    recommended_conversation_status: str | None = None


class StrictCitationCheckResult(BaseModel):
    is_valid: bool
    cited_pairs: list[str] = Field(default_factory=list)
    uncited_sentences: list[str] = Field(default_factory=list)
    invalid_citations: list[str] = Field(default_factory=list)


class RerankerProtocol(Protocol):
    async def rerank(self, query: str, candidates: list[RagCandidate], top_k: int) -> list[RagHit]:
        """Score and rank retrieved candidates into [0.0, 1.0] normalized RagHits."""


def build_websearch_tsquery(query: str) -> str | None:
    tokens: list[str] = []
    seen: set[str] = set()
    lowered = query.lower()

    for sku in _SKU_OR_CODE_RE.findall(lowered):
        cleaned = re.sub(r"[^a-z0-9\-_.]", "", sku)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tokens.append(f'"{cleaned}"' if "-" in cleaned else cleaned)

    for word in _ALNUM_WORD_RE.findall(lowered):
        cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", word)
        if not cleaned or cleaned in _STOPWORDS:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            tokens.append(cleaned)

    if not tokens:
        return None
    return " OR ".join(tokens[:24])


def _extract_meaningful_terms(text_value: str) -> tuple[set[str], set[str]]:
    lowered = text_value.lower()
    terms: set[str] = set()
    codes: set[str] = set()

    for sku in _SKU_OR_CODE_RE.findall(lowered):
        terms.add(sku)
        codes.add(sku)

    for w in _ALNUM_WORD_RE.findall(lowered):
        if w in _STOPWORDS:
            continue
        terms.add(w)
        if any(ch.isdigit() for ch in w) and len(w) >= 3:
            codes.add(w)
        sub_parts = _ALPHA_DIGIT_SPLIT_RE.findall(w)
        if len(sub_parts) > 1:
            for sp in sub_parts:
                if len(sp) >= 2 and sp not in _STOPWORDS:
                    terms.add(sp)

    return terms, codes


class LocalHybridReranker:
    """Score exact SKU matches, lexical recall and vector similarity."""

    async def rerank(self, query: str, candidates: list[RagCandidate], top_k: int) -> list[RagHit]:
        q_terms, q_codes = _extract_meaningful_terms(query)
        scored: list[RagHit] = []

        for cand in candidates:
            c_lower = cand.raw_content.lower()
            c_terms, _ = _extract_meaningful_terms(cand.raw_content)

            if q_terms:
                matched_count = sum(1 for t in q_terms if t in c_terms or t in c_lower)
                token_recall = matched_count / len(q_terms)
            else:
                token_recall = 0.0

            code_adjustment = 0.0
            if q_codes:
                code_adjustment = 0.25 if any(code in c_lower for code in q_codes) else -0.25

            cos_clamped = max(0.0, min(1.0, cand.cosine_sim))
            bm25_boost = min(0.10, max(0.0, cand.bm25_score * 0.15))
            raw_score = 0.55 * token_recall + 0.35 * cos_clamped + bm25_boost + code_adjustment
            rerank_score = round(max(0.0, min(1.0, raw_score)), 4)

            safe_content = (
                strip_dynamic_prices_for_answer(cand.raw_content)
                if cand.has_dynamic_price_disclaimer
                else cand.raw_content
            )
            scored.append(
                RagHit(
                    chunk_id=cand.chunk_id,
                    doc_id=cand.doc_id,
                    doc_title=cand.doc_title,
                    chunk_index=cand.chunk_index,
                    content=safe_content,
                    has_dynamic_price_disclaimer=cand.has_dynamic_price_disclaimer,
                    rrf_score=cand.rrf_score,
                    bm25_score=cand.bm25_score,
                    cosine_sim=cand.cosine_sim,
                    rerank_score=rerank_score,
                )
            )

        scored.sort(key=lambda item: (item.rerank_score, item.rrf_score), reverse=True)
        return scored[:top_k]


class HttpCrossEncoderReranker:
    """Adapter for hosted cross-encoder rerank APIs (e.g., BGE-Reranker-v2-m3 / Cohere)."""

    def __init__(self, endpoint_url: str, api_key: str, model: str = "bge-reranker-v2-m3") -> None:
        self.endpoint_url = endpoint_url
        self.api_key = api_key
        self.model = model
        self._fallback = LocalHybridReranker()

    async def rerank(self, query: str, candidates: list[RagCandidate], top_k: int) -> list[RagHit]:
        if not candidates:
            return []
        if not self.endpoint_url or not self.api_key:
            return await self._fallback.rerank(query, candidates, top_k)

        docs = [c.raw_content for c in candidates]
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.endpoint_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "query": query, "documents": docs, "top_n": top_k},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])

        ranked: list[RagHit] = []
        for entry in results:
            cand = candidates[int(entry["index"])]
            score = round(max(0.0, min(1.0, float(entry["relevance_score"]))), 4)
            safe_content = (
                strip_dynamic_prices_for_answer(cand.raw_content)
                if cand.has_dynamic_price_disclaimer
                else cand.raw_content
            )
            ranked.append(
                RagHit(
                    chunk_id=cand.chunk_id,
                    doc_id=cand.doc_id,
                    doc_title=cand.doc_title,
                    chunk_index=cand.chunk_index,
                    content=safe_content,
                    has_dynamic_price_disclaimer=cand.has_dynamic_price_disclaimer,
                    rrf_score=cand.rrf_score,
                    bm25_score=cand.bm25_score,
                    cosine_sim=cand.cosine_sim,
                    rerank_score=score,
                )
            )
        ranked.sort(key=lambda item: item.rerank_score, reverse=True)
        return ranked[:top_k]


def verify_strict_citations(
    reply_text: str, retrieved_hits: list[RagHit]
) -> StrictCitationCheckResult:
    """Require valid [doc_id#chunk_id] citations on each claim in strict mode."""
    allowed_pairs = {(str(h.doc_id), str(h.chunk_id)) for h in retrieved_hits}
    uncited: list[str] = []
    invalid = _MALFORMED_CITATION_RE.findall(_INLINE_CITATION_RE.sub("", reply_text))
    cited: list[str] = []

    raw_sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(reply_text.strip()) if s.strip()]
    for sentence in raw_sentences:
        if not any(ch.isalnum() for ch in _INLINE_CITATION_RE.sub("", sentence)):
            continue
        matches = _INLINE_CITATION_RE.findall(sentence)
        if not matches:
            uncited.append(sentence)
            continue
        for doc_id_str, chunk_id_str in matches:
            doc_id_str, chunk_id_str = doc_id_str.lower(), chunk_id_str.lower()
            pair_token = f"{doc_id_str}#{chunk_id_str}"
            if (doc_id_str, chunk_id_str) not in allowed_pairs:
                invalid.append(pair_token)
            else:
                cited.append(pair_token)

    return StrictCitationCheckResult(
        is_valid=(len(uncited) == 0 and len(invalid) == 0 and len(cited) > 0),
        cited_pairs=cited,
        uncited_sentences=uncited,
        invalid_citations=invalid,
    )


class HybridRagService:
    """Single-CTE RRF (k=60) BM25 + pgvector HNSW search with reranking and strict abstention."""

    def __init__(
        self,
        embedding_provider: EmbeddingProviderProtocol | None = None,
        reranker: RerankerProtocol | None = None,
        abstention_threshold: float = ABSTENTION_THRESHOLD,
    ) -> None:
        self.embedding_provider = embedding_provider or OpenAIEmbeddingProvider()
        self.reranker = reranker or LocalHybridReranker()
        self.abstention_threshold = abstention_threshold

    async def search(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        query: str,
        top_k: int = 5,
        strict_mode: bool = False,
    ) -> RagSearchResult:
        embeddings = await self.embedding_provider.embed_texts([query])
        query_vec_literal = format_pgvector_literal(embeddings[0])
        websearch_q = build_websearch_tsquery(query)

        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20.")

        rows = (
            (
                await session.execute(
                    text(
                        """
                    WITH q AS (
                        SELECT websearch_to_tsquery('simple', :ws_query) AS tsq,
                               CAST(CAST(:q_emb AS text) AS vector) AS vec
                    ), corpus_stats AS (
                        SELECT count(*) AS n, greatest(avg(c.token_count), 1) AS avgdl
                        FROM kb_chunks c
                        JOIN kb_documents d ON (d.tenant_id, d.id) = (c.tenant_id, c.document_id)
                        WHERE c.tenant_id = :tid AND d.is_active
                    ), term_hits AS (
                        SELECT c.id, c.token_count AS dl, term.lexeme,
                               cardinality(term.positions) AS tf,
                               count(*) OVER (PARTITION BY term.lexeme) AS df
                        FROM kb_chunks c
                        JOIN kb_documents d ON (d.tenant_id, d.id) = (c.tenant_id, c.document_id)
                        CROSS JOIN q
                        CROSS JOIN LATERAL unnest(c.tsv) AS term
                        WHERE c.tenant_id = :tid AND d.is_active AND c.tsv @@ q.tsq
                          AND (term.lexeme = ANY(tsvector_to_array(
                              to_tsvector('simple', replace(:ws_query, ' OR ', ' ')))))
                    ), lexical AS (
                        SELECT id, sum(
                            ln(1.0 + (n - df + 0.5) / (df + 0.5)) *
                            (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * dl / avgdl))
                        ) AS bm25_score
                        FROM term_hits CROSS JOIN corpus_stats
                        GROUP BY id
                        ORDER BY bm25_score DESC, id LIMIT 40
                    ), fts_candidates AS (
                        SELECT *, row_number() OVER (ORDER BY bm25_score DESC, id) AS rnk
                        FROM lexical
                    ), nearest AS (
                        SELECT c.id, c.embedding <=> q.vec AS distance
                        FROM kb_chunks c
                        JOIN kb_documents d ON (d.tenant_id, d.id) = (c.tenant_id, c.document_id)
                        CROSS JOIN q
                        WHERE c.tenant_id = :tid AND d.is_active
                        ORDER BY c.embedding <=> q.vec LIMIT 40
                    ), vec_candidates AS (
                        SELECT id, row_number() OVER (ORDER BY distance, id) AS rnk,
                               1.0 - distance AS cosine_sim
                        FROM nearest WHERE distance < 2.0
                    ),
                    fused AS (
                        SELECT
                            COALESCE(f.id, v.id) AS chunk_id,
                            (COALESCE(1.0 / (60.0 + f.rnk), 0.0) + COALESCE(1.0 / (60.0 +
                                v.rnk), 0.0)) AS rrf_score,
                            COALESCE(f.bm25_score, 0.0) AS bm25_score,
                            COALESCE(v.cosine_sim, 0.0) AS cosine_sim
                        FROM fts_candidates f
                        FULL OUTER JOIN vec_candidates v ON f.id = v.id
                    )
                    SELECT
                        c.id AS chunk_id,
                        c.document_id AS doc_id,
                        d.title AS doc_title,
                        c.chunk_index,
                        c.content,
                        c.has_dynamic_price_disclaimer,
                        fused.rrf_score::double precision AS rrf_score,
                        fused.bm25_score::double precision AS bm25_score,
                        fused.cosine_sim::double precision AS cosine_sim
                    FROM fused
                    JOIN kb_chunks c ON c.id = fused.chunk_id AND c.tenant_id = :tid
                    JOIN kb_documents d ON d.id = c.document_id AND d.tenant_id = :tid
                    ORDER BY fused.rrf_score DESC, c.id
                    LIMIT 40;
                    """
                    ),
                    {"tid": tenant_id, "ws_query": websearch_q, "q_emb": query_vec_literal},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            return RagSearchResult(
                query=query,
                strict_mode=strict_mode,
                hits=[],
                top_score=0.0,
                should_abstain=True,
                abstention_reason="no_kb_hits",
                recommended_conversation_status=ConversationStatus.PENDING_HUMAN.value,
            )

        candidates = [
            RagCandidate(
                chunk_id=r["chunk_id"],
                doc_id=r["doc_id"],
                doc_title=r["doc_title"],
                chunk_index=int(r["chunk_index"]),
                raw_content=r["content"],
                has_dynamic_price_disclaimer=bool(r["has_dynamic_price_disclaimer"]),
                rrf_score=float(r["rrf_score"]),
                bm25_score=float(r["bm25_score"]),
                cosine_sim=float(r["cosine_sim"]),
            )
            for r in rows
        ]

        hits = await self.reranker.rerank(query, candidates, top_k=top_k)
        if strict_mode:
            hits = [hit for hit in hits if hit.rerank_score >= self.abstention_threshold]
        top_score = hits[0].rerank_score if hits else 0.0
        should_abstain = (not hits) or (top_score < self.abstention_threshold)

        return RagSearchResult(
            query=query,
            strict_mode=strict_mode,
            hits=hits,
            top_score=top_score,
            should_abstain=should_abstain,
            abstention_reason="top_rerank_score_below_threshold" if should_abstain else None,
            recommended_conversation_status=(
                ConversationStatus.PENDING_HUMAN.value if should_abstain else None
            ),
        )

    async def apply_abstention_handoff(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> None:
        await session.execute(
            text(
                "UPDATE conversations "
                "SET status = :status, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {
                "status": ConversationStatus.PENDING_HUMAN.value,
                "tid": tenant_id,
                "cid": conversation_id,
            },
        )
