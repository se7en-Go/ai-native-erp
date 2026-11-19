from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.ingestion import DocumentIngestionService
from erp_core.ai_engine.rag_service import HybridRagService, RagSearchResult
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/ai/kb", tags=["ai-kb"])
_editor = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER))
_reader = Depends(require_roles(*UserRole))


def _resolve_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_factory)


class KbDocumentRead(BaseModel):
    id: UUID
    title: str
    source_type: str
    source_uri: str | None = None
    language: str
    is_active: bool
    status: str = "indexed"
    chunk_count: int
    flagged_price_chunks: int
    created_at: datetime


class KbIngestResponse(BaseModel):
    document_id: UUID
    chunks_created: int
    flagged_dynamic_price_chunks: int
    deduplicated: bool


class KbSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    strict_mode: bool = False
    conversation_id: UUID | None = None


async def _get_tenant_db(
    request: Request,
    principal: Annotated[CurrentPrincipal, _reader],
) -> AsyncIterator[tuple[AsyncSession, CurrentPrincipal]]:
    factory = _resolve_session_factory(request)
    async with tenant_session(factory, principal.tenant_id) as session:
        yield session, principal


@router.post(
    "/documents",
    response_model=KbIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_kb_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    title: str = Form(..., min_length=1, max_length=255),
    source_type: Literal["markdown", "pdf", "csv", "faq_json", "policy_doc"] = Form("markdown"),
    language: str = Form("en"),
    principal: CurrentPrincipal = _editor,
) -> KbIngestResponse:
    max_bytes = 8 * 1024 * 1024
    raw_bytes = await file.read(max_bytes + 1)
    if len(raw_bytes) > max_bytes:
        raise AppError(
            slug="kb-file-too-large",
            status_code=413,
            code="ERR_KB_FILE_TOO_LARGE",
            title="Knowledge Base File Too Large",
            detail="Maximum upload size is 8 MiB.",
        )
    if not raw_bytes:
        raise AppError(
            slug="empty-kb-file",
            status_code=status.HTTP_400_BAD_REQUEST,
            code="ERR_EMPTY_KB_FILE",
            title="Empty Knowledge Base File",
            detail="Uploaded file contains zero bytes.",
        )

    factory = _resolve_session_factory(request)
    ingestion_svc = DocumentIngestionService()
    async with tenant_session(factory, principal.tenant_id) as session:
        try:
            result = await ingestion_svc.ingest_document(
                session,
                tenant_id=principal.tenant_id,
                title=title,
                source_type=source_type,
                raw_bytes=raw_bytes,
                language=language,
                source_uri=file.filename,
            )
        except ValueError as exc:
            raise AppError(
                slug="kb-parse-failed",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="ERR_KB_PARSE_FAILED",
                title="Knowledge Base Document Parse Failed",
                detail="The document could not be parsed or indexed.",
            ) from exc

    return KbIngestResponse(
        document_id=result.document_id,
        chunks_created=result.chunks_created,
        flagged_dynamic_price_chunks=result.flagged_dynamic_price_chunks,
        deduplicated=result.deduplicated,
    )


@router.get("/documents", response_model=PaginatedResponse[KbDocumentRead])
async def list_kb_documents(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: tuple[AsyncSession, CurrentPrincipal] = Depends(_get_tenant_db),
) -> PaginatedResponse[KbDocumentRead]:
    session, principal = ctx
    total = (
        await session.execute(
            text("SELECT COUNT(*) FROM kb_documents WHERE tenant_id = :tid AND is_active = true"),
            {"tid": principal.tenant_id},
        )
    ).scalar_one()

    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    d.id,
                    d.title,
                    d.source_type,
                    d.source_uri,
                    d.language,
                    d.is_active,
                    d.created_at,
                    COUNT(c.id) AS chunk_count,
                    COALESCE(SUM(CASE WHEN c.has_dynamic_price_disclaimer THEN 1 ELSE 0 END), 0)
                        AS flagged_price_chunks
                FROM kb_documents d
                LEFT JOIN kb_chunks c ON c.document_id = d.id AND c.tenant_id = d.tenant_id
                WHERE d.tenant_id = :tid AND d.is_active = true
                GROUP BY d.id
                ORDER BY d.created_at DESC, d.id
                LIMIT :limit OFFSET :offset
                """
                ),
                {"tid": principal.tenant_id, "limit": limit, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    items = [
        KbDocumentRead(
            id=r["id"],
            title=r["title"],
            source_type=r["source_type"],
            source_uri=r["source_uri"],
            language=r["language"],
            is_active=bool(r["is_active"]),
            status="indexed" if bool(r["is_active"]) else "archived",
            chunk_count=int(r["chunk_count"]),
            flagged_price_chunks=int(r["flagged_price_chunks"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return PaginatedResponse(
        items=items,
        total=int(total),
        limit=limit,
        offset=offset,
        has_more=(offset + len(items) < int(total)),
    )


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_kb_document(
    doc_id: UUID,
    request: Request,
    principal: CurrentPrincipal = _editor,
) -> None:
    factory = _resolve_session_factory(request)
    async with tenant_session(factory, principal.tenant_id) as session:
        updated = (
            await session.execute(
                text(
                    "UPDATE kb_documents SET is_active = false, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :doc_id RETURNING id"
                ),
                {"tid": principal.tenant_id, "doc_id": doc_id},
            )
        ).scalar_one_or_none()
        if updated is None:
            raise AppError(
                slug="kb-document-not-found",
                status_code=status.HTTP_404_NOT_FOUND,
                code="ERR_KB_DOCUMENT_NOT_FOUND",
                title="KB Document Not Found",
                detail=f"Document {doc_id} was not found for this tenant.",
            )


@router.post("/search", response_model=RagSearchResult)
async def search_kb_hybrid(
    body: KbSearchRequest,
    ctx: tuple[AsyncSession, CurrentPrincipal] = Depends(_get_tenant_db),
) -> RagSearchResult:
    session, principal = ctx
    rag = HybridRagService()
    result = await rag.search(
        session,
        tenant_id=principal.tenant_id,
        query=body.query,
        top_k=body.top_k,
        strict_mode=body.strict_mode,
    )
    if result.should_abstain and body.conversation_id is not None:
        await rag.apply_abstention_handoff(
            session,
            tenant_id=principal.tenant_id,
            conversation_id=body.conversation_id,
        )
    return result
