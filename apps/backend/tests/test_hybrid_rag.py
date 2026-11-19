from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from erp_core.ai_engine.ingestion import (
    DeterministicHashEmbeddingProvider,
    DocumentIngestionService,
    OpenAIEmbeddingProvider,
)
from erp_core.ai_engine.rag_service import (
    ABSTENTION_THRESHOLD,
    HybridRagService,
    LocalHybridReranker,
    verify_strict_citations,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)


async def test_embeddings_use_same_configured_openai_endpoint_as_chat(monkeypatch) -> None:
    async def post(_client, url, *, headers, json):
        assert url == "http://127.0.0.1:9099/v1/embeddings"
        assert headers == {"Authorization": "Bearer fixture-key"}
        assert json["input"] == ["LED strip warranty"]
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(get_settings(), "OPENAI_BASE_URL", "http://127.0.0.1:9099/v1/")
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider = OpenAIEmbeddingProvider(api_key="fixture-key")
    result = await provider.embed_texts(["LED strip warranty"])
    assert result == [[1.0]]


@pytest.fixture
async def runtime_engine() -> AsyncEngine:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_rag_bm25_vector_price_redaction_strict_mode_and_abstention(
    runtime_engine: AsyncEngine,
) -> None:
    tenant_id = uuid4()
    conv_id = uuid4()
    customer_id = uuid4()
    channel_acct_id = uuid4()
    runtime_factory = create_session_factory(runtime_engine)

    async with tenant_session(runtime_factory, tenant_id) as setup_sess:
        await setup_sess.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": "Quanzhou LED", "slug": f"qz-{tenant_id.hex[:8]}"},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
                "display_name, status, rate_limit_profile) "
                "VALUES (:id, :tid, 'web_livechat', :ext, 'LED Site', 'active', '{\"mps\": "
                "50}'::jsonb)"
            ),
            {"id": channel_acct_id, "tid": tenant_id, "ext": f"site-{tenant_id.hex[:6]}"},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', 'Marco Rossi')"
            ),
            {"id": customer_id, "tid": tenant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, conversation_mode, status) "
                "VALUES (:id, :tid, :cid, :caid, 'web_livechat', 'b2c_support', 'open')"
            ),
            {"id": conv_id, "tid": tenant_id, "cid": customer_id, "caid": channel_acct_id},
        )

    try:
        embedder = DeterministicHashEmbeddingProvider()
        ingestion = DocumentIngestionService(embedding_provider=embedder)
        rag = HybridRagService(
            embedding_provider=embedder,
            reranker=LocalHybridReranker(),
        )

        spec_md = (
            "# XJ-400Pro-EU Floodlight Specification\n"
            "Model XJ-400Pro-EU is a 400W outdoor stadium LED floodlight with IP66 waterproof "
            "housing.\n"
            "Warranty period is 5 years against driver and LED chip failure. Certified CE, RoHS, "
            "and ENEC.\n"
            "HS Code 9405429000. Aluminum die-cast heat sink with MeanWell driver."
            "\n工业防水投光灯，适用于体育场，质保五年。"
        ).encode()

        stale_price_csv = (
            b"sku,moq,fob_price_usd,stock_note\nXJ-400Pro-EU,500,$12.99,Old 2024 price list\n"
        )

        async with tenant_session(runtime_factory, tenant_id) as session:
            spec_res = await ingestion.ingest_document(
                session,
                tenant_id=tenant_id,
                title="XJ-400Pro-EU Technical Datasheet",
                source_type="markdown",
                raw_bytes=spec_md,
            )
            price_res = await ingestion.ingest_document(
                session,
                tenant_id=tenant_id,
                title="Legacy 2024 Price Sheet",
                source_type="csv",
                raw_bytes=stale_price_csv,
            )
            assert spec_res.chunks_created >= 1
            assert price_res.flagged_dynamic_price_chunks == 1

            # 1. Exact SKU match via BM25 + semantic fusion
            hit_res = await rag.search(
                session,
                tenant_id=tenant_id,
                query="What is the IP rating and warranty of XJ-400Pro-EU?",
                top_k=5,
                strict_mode=True,
            )
            assert hit_res.should_abstain is False
            assert hit_res.top_score >= ABSTENTION_THRESHOLD
            assert len(hit_res.hits) >= 1
            top_hit = hit_res.hits[0]
            assert "IP66" in top_hit.content
            assert top_hit.bm25_score > 0 and top_hit.cosine_sim > 0
            assert top_hit.rrf_score > 1 / 61
            chinese = await rag.search(
                session, tenant_id, "工业防水投光灯质保五年", strict_mode=True
            )
            assert not chinese.should_abstain
            assert chinese.hits[0].doc_id == spec_res.document_id
            assert chinese.hits[0].bm25_score > 0
            assert "$12.99" not in " ".join(h.content for h in hit_res.hits)

            # Verify pricing chunk stripped static dollar amount
            price_search = await rag.search(
                session,
                tenant_id=tenant_id,
                query="XJ-400Pro-EU FOB price list 500",
                top_k=5,
                strict_mode=False,
            )
            flagged_hits = [h for h in price_search.hits if h.has_dynamic_price_disclaimer]
            assert len(flagged_hits) >= 1
            assert "$12.99" not in flagged_hits[0].content
            assert "500" not in flagged_hits[0].content
            assert "[PRICE_REDACTED_USE_LIVE_ERP_TOOL]" in flagged_hits[0].content

            # 2. Strict Retrieval Mode inline citation check
            valid_cite = f"[{top_hit.doc_id}#{top_hit.chunk_id}]"
            ok_check = verify_strict_citations(
                f"XJ-400Pro-EU has an IP66 waterproof rating and 5-year warranty {valid_cite}.",
                hit_res.hits,
            )
            assert ok_check.is_valid is True

            uncited_check = verify_strict_citations(
                "XJ-400Pro-EU has an IP66 waterproof rating. It also ships free worldwide.",
                hit_res.hits,
            )
            assert uncited_check.is_valid is False
            assert len(uncited_check.uncited_sentences) == 2

            fake_cite_check = verify_strict_citations(
                f"XJ-400Pro-EU supports IP66 [{uuid4()}#{uuid4()}].",
                hit_res.hits,
            )
            assert fake_cite_check.is_valid is False
            assert len(fake_cite_check.invalid_citations) == 1
            assert not verify_strict_citations(
                f"Untested.Includes a warranty {valid_cite}.", hit_res.hits
            ).is_valid
            assert not verify_strict_citations(
                f"Includes a warranty [fake#fake] {valid_cite}.", hit_res.hits
            ).is_valid
            assert not verify_strict_citations(
                f"支持。未经证实{valid_cite}。", hit_res.hits
            ).is_valid
            assert not verify_strict_citations(f"{valid_cite}。", hit_res.hits).is_valid

            # 3. Low-confidence (< 0.62) triggers abstention and marks conversation pending_human
            abstain_res = await rag.search(
                session,
                tenant_id=tenant_id,
                query="Do you sell nuclear reactor coolant valves for submarines?",
                top_k=5,
                strict_mode=True,
            )
            assert abstain_res.should_abstain is True
            assert abstain_res.top_score < ABSTENTION_THRESHOLD
            assert abstain_res.recommended_conversation_status == "pending_human"

            await rag.apply_abstention_handoff(
                session, tenant_id=tenant_id, conversation_id=conv_id
            )
            status = (
                await session.execute(
                    text("SELECT status FROM conversations WHERE id = :cid"),
                    {"cid": conv_id},
                )
            ).scalar_one()
            assert status == "pending_human"

            terms = await ingestion.ingest_document(
                session,
                tenant_id=tenant_id,
                title="Lexical length",
                source_type="markdown",
                raw_bytes="IP66 防水灯 water water".encode(),
            )
            assert (
                await session.scalar(
                    text("SELECT token_count FROM kb_chunks WHERE document_id = :id"),
                    {"id": terms.document_id},
                )
                == 6
            )

        async with tenant_session(runtime_factory, uuid4()) as other_session:
            foreign = await rag.search(other_session, tenant_id, "工业防水投光灯质保五年")
            assert foreign.should_abstain and not foreign.hits
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        finally:
            await admin.dispose()
