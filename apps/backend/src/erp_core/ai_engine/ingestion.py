from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.config import get_settings

EMBEDDING_DIM = 1536
_PRICE_REDACTION = "[PRICE_REDACTED_USE_LIVE_ERP_TOOL]"

_PRICING_KEYWORD_RE = re.compile(
    r"(?:[$€£¥]\s*\d+|\b(?:USD|EUR|GBP|CNY|RMB)\s*\d+|\d+\s*(?:USD|EUR|GBP|CNY|RMB)\b|"
    r"\b(?:unit\s*price|fob\s*price|cif\s*price|exw\s*price|ddp\s*price|"
    r"wholesale\s*price|retail\s*price|price\s*list|moq\s*tier|fob_price_usd)\b|"
    r"\b(?:moq|stock|inventory|eta|lead[ _-]time|tracking[ _-]number)\b|"
    r"(?:价格|报价|单价|阶梯价|出厂价|起订量|库存|交期|单号))",
    re.IGNORECASE,
)
_SKU_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]")


class EmbeddingProviderProtocol(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into 1536-dimensional L2-normalized vectors."""


class DeterministicHashEmbeddingProvider:
    """Deterministic 1536-dim feature-hashed embedding provider for offline tests and evals."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed_one(self, text_value: str) -> list[float]:
        vec = [0.0] * self.dim
        lowered = text_value.lower()
        features: list[tuple[str, float]] = []

        for sku in _SKU_TOKEN_RE.findall(lowered):
            features.append((f"sku:{sku}", 3.0))
        words = _WORD_TOKEN_RE.findall(lowered)
        for w in words:
            features.append((f"w:{w}", 1.5))
            if len(w) >= 4:
                for i in range(len(w) - 2):
                    features.append((f"tri:{w[i : i + 3]}", 0.5))
        for i in range(len(words) - 1):
            features.append((f"bi:{words[i]}_{words[i + 1]}", 1.2))

        if not features:
            features.append(("empty", 1.0))

        for feat, weight in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            raw_int = int.from_bytes(digest, "little", signed=False)
            bucket = raw_int % self.dim
            sign = 1.0 if ((raw_int >> 63) & 1) == 0 else -1.0
            vec[bucket] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return [0.0] * self.dim
        return [round(v / norm, 7) for v in vec]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class OpenAIEmbeddingProvider:
    """Async OpenAI text-embedding-3-small client with deterministic fallback if unconfigured."""

    def __init__(self, api_key: str | None = None, model: str = "text-embedding-3-small") -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.base_url = settings.OPENAI_BASE_URL.rstrip("/")
        self.model = model
        self._fallback = DeterministicHashEmbeddingProvider()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key or self.api_key.startswith("test-"):
            return await self._fallback.embed_texts(texts)

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            payload = resp.json()
            ordered = sorted(payload["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in ordered]


def has_dynamic_pricing_signals(text_value: str) -> bool:
    return bool(_PRICING_KEYWORD_RE.search(text_value))


def strip_dynamic_prices_for_answer(text_value: str) -> str:
    return "\n".join(
        _PRICE_REDACTION if has_dynamic_pricing_signals(line) else line
        for line in text_value.splitlines()
    )


def extract_text_from_upload(raw_bytes: bytes, source_type: str) -> str:
    if source_type in {"markdown", "policy_doc"}:
        return raw_bytes.decode("utf-8", errors="replace").strip()

    if source_type == "csv":
        decoded = raw_bytes.decode("utf-8", errors="replace")
        csv_reader = csv.DictReader(io.StringIO(decoded))
        lines: list[str] = []
        for idx, row in enumerate(csv_reader):
            parts = [f"{k}: {v}" for k, v in row.items() if k is not None and v is not None]
            if parts:
                lines.append(f"Row {idx + 1} | " + " | ".join(parts))
        return "\n".join(lines).strip()

    if source_type == "faq_json":
        data = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(data, list):
            blocks: list[str] = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("FAQ JSON entries must be objects.")
                q = str(item.get("question", "")).strip()
                a = str(item.get("answer", "")).strip()
                if q or a:
                    blocks.append(f"Q: {q}\nA: {a}")
            return "\n\n".join(blocks).strip()
        return json.dumps(data, ensure_ascii=False, indent=2)

    if source_type == "pdf":
        try:
            reader = PdfReader(io.BytesIO(raw_bytes))
            if reader.is_encrypted or len(reader.pages) > 250:
                raise ValueError("PDF must be unencrypted and at most 250 pages.")
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except PyPdfError as exc:
            raise ValueError("Invalid PDF document.") from exc
        joined = "\n\n".join(p for p in pages if p)
        if not joined:
            raise ValueError("Uploaded PDF contains no extractable text.")
        return joined

    raise ValueError(f"Unsupported KB document source_type: {source_type}")


def chunk_text(text_value: str, max_chars: int = 1200, overlap_chars: int = 150) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", text_value).strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("Invalid chunk overlap.")
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        if end < len(normalized):
            newline_pos = normalized.rfind("\n", start + int(max_chars * 0.5), end)
            if newline_pos > start:
                end = newline_pos
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        next_start = max(start + 1, end - overlap_chars)
        start = next_start
    return chunks


def format_pgvector_literal(vec: list[float]) -> str:
    if len(vec) != EMBEDDING_DIM or not all(math.isfinite(x) for x in vec):
        raise ValueError("Invalid embedding dimensions or non-finite values.")
    return "[" + ",".join(repr(x) for x in vec) + "]"


@dataclass(slots=True)
class KbIngestResult:
    document_id: UUID
    chunks_created: int
    flagged_dynamic_price_chunks: int
    deduplicated: bool


class DocumentIngestionService:
    """Parses, chunks, flags pricing-disclaimer chunks, embeds, and persists KB documents."""

    def __init__(self, embedding_provider: EmbeddingProviderProtocol | None = None) -> None:
        self.embedding_provider = embedding_provider or OpenAIEmbeddingProvider()

    async def ingest_document(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        title: str,
        source_type: str,
        raw_bytes: bytes,
        language: str = "en",
        source_uri: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> KbIngestResult:
        extracted = extract_text_from_upload(raw_bytes, source_type)
        if len(extracted) > 400_000:
            raise ValueError("Extracted document is too large.")
        chunks = chunk_text(strip_dynamic_prices_for_answer(extracted))
        if not chunks:
            raise ValueError("Document produced zero non-empty text chunks.")

        doc_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing_id = (
            await session.execute(
                text(
                    "SELECT id FROM kb_documents "
                    "WHERE tenant_id = :tid AND doc_hash = :doc_hash AND is_active = true "
                    "LIMIT 1"
                ),
                {"tid": tenant_id, "doc_hash": doc_hash},
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            existing_count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM kb_chunks WHERE document_id = :did"),
                    {"did": existing_id},
                )
            ).scalar_one()
            flagged_cnt = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM kb_chunks "
                        "WHERE document_id = :did AND has_dynamic_price_disclaimer = true"
                    ),
                    {"did": existing_id},
                )
            ).scalar_one()
            return KbIngestResult(
                document_id=existing_id,
                chunks_created=int(existing_count),
                flagged_dynamic_price_chunks=int(flagged_cnt),
                deduplicated=True,
            )

        flagged = [_PRICE_REDACTION in chunk for chunk in chunks]
        embeddings: list[list[float]] = []
        for start in range(0, len(chunks), 64):
            embeddings.extend(await self.embedding_provider.embed_texts(chunks[start : start + 64]))
        pg_vectors = [format_pgvector_literal(vec) for vec in embeddings]
        if len(pg_vectors) != len(chunks):
            raise ValueError("Embedding provider returned an unexpected number of vectors.")
        doc_id = uuid4()
        meta_str = json.dumps(metadata_json or {})
        await session.execute(
            text(
                "INSERT INTO kb_documents (id, tenant_id, title, source_type, source_uri, "
                "doc_hash, language, is_active, metadata_json) "
                "VALUES (:id, :tid, :title, :source_type, :source_uri, :doc_hash, :language, "
                "true, CAST(:meta AS jsonb))"
            ),
            {
                "id": doc_id,
                "tid": tenant_id,
                "title": title,
                "source_type": source_type,
                "source_uri": source_uri,
                "doc_hash": doc_hash,
                "language": language,
                "meta": meta_str,
            },
        )

        for idx, (chunk_str, emb, has_price_flag) in enumerate(
            zip(chunks, pg_vectors, flagged, strict=True)
        ):
            await session.execute(
                text(
                    r"""
                    INSERT INTO kb_chunks
                        (id, tenant_id, document_id, chunk_index, content, embedding,
                         has_dynamic_price_disclaimer, token_count)
                    SELECT :id, :tid, :doc_id, :idx, :content,
                           CAST(CAST(:emb AS text) AS vector), :flag,
                           COALESCE(sum(cardinality(term.positions)), 0)
                    FROM unnest(to_tsvector('simple', regexp_replace(
                        CAST(:content AS text), '([一-龯])', ' \1 ', 'g'
                    ))) AS term
                    """
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "doc_id": doc_id,
                    "idx": idx,
                    "content": chunk_str,
                    "emb": emb,
                    "flag": has_price_flag,
                },
            )

        return KbIngestResult(
            document_id=doc_id,
            chunks_created=len(chunks),
            flagged_dynamic_price_chunks=sum(flagged),
            deduplicated=False,
        )
