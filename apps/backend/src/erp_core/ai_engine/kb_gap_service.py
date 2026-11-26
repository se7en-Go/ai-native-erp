from __future__ import annotations

import hashlib
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.ingestion import (
    DeterministicHashEmbeddingProvider,
    format_pgvector_literal,
    has_dynamic_pricing_signals,
    strip_dynamic_prices_for_answer,
)
from erp_core.ai_engine.kb_gap_schemas import (
    AdoptionTypeMetrics,
    AiAdoptionMetrics,
    KbGapCreateRequest,
    KbGapPromoteResult,
    KbGapRead,
    RedlineCheckResult,
    RedlineMatchedRule,
    RedlineRuleCreate,
    RedlineRuleRead,
)
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event

_MAX_PATTERN_LENGTH = 200
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]")
_STATUS_TOKENS = (
    "pending",
    "approved",
    "edited_and_approved",
    "rejected",
    "expired",
    "failed",
)

_RULE_COLUMNS = (
    "SELECT id, tenant_id, name, pattern, match_type, action, is_enabled, created_at "
    "FROM outbound_redline_rules"
)
_GAP_COLUMNS = (
    "SELECT id, tenant_id, conversation_id, customer_question, detected_language, "
    "frequency_count, status, resolved_answer, promoted_kb_document_id, "
    "created_at, updated_at FROM kb_gap_items"
)


def _invalid_regex(pattern: str) -> AppError:
    return AppError(
        slug="invalid-redline-pattern",
        status_code=422,
        code="ERR_INVALID_REDLINE_PATTERN",
        title="Invalid Red Line Pattern",
        detail=(
            "Regex red-line patterns must be at most "
            f"{_MAX_PATTERN_LENGTH} characters and must compile."
        ),
    )


def _gap_not_found(gap_id: UUID) -> AppError:
    return AppError(
        slug="kb-gap-not-found",
        status_code=404,
        code="ERR_KB_GAP_NOT_FOUND",
        title="Knowledge Base Gap Not Found",
        detail=f"Knowledge base gap '{gap_id}' does not exist in this workspace.",
    )


def _rule_not_found(rule_id: UUID) -> AppError:
    return AppError(
        slug="redline-rule-not-found",
        status_code=404,
        code="ERR_REDLINE_RULE_NOT_FOUND",
        title="Red Line Rule Not Found",
        detail=f"Red line rule '{rule_id}' does not exist in this workspace.",
    )


def _rule_from_row(row: Row[Any]) -> RedlineRuleRead:
    m = row._mapping
    return RedlineRuleRead(
        id=m["id"],
        tenant_id=m["tenant_id"],
        name=m["name"],
        pattern=m["pattern"],
        match_type=m["match_type"],
        action=m["action"],
        is_enabled=bool(m["is_enabled"]),
        created_at=m["created_at"],
    )


def _gap_from_row(row: Row[Any]) -> KbGapRead:
    m = row._mapping
    return KbGapRead(
        id=m["id"],
        tenant_id=m["tenant_id"],
        conversation_id=m["conversation_id"],
        customer_question=m["customer_question"],
        detected_language=m["detected_language"],
        frequency_count=int(m["frequency_count"]),
        status=m["status"],
        resolved_answer=m["resolved_answer"],
        promoted_kb_document_id=m["promoted_kb_document_id"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def _pct(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0.00")
    return (Decimal(numerator) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


class KbGapService:
    """Outbound red-line guard, knowledge-base gap backlog, and AI adoption metrics.

    Lives in `erp_core.ai_engine` because every table it touches (`kb_documents`,
    `kb_chunks`, `ai_proposals`, plus its own two tables) belongs to the AI
    module; the tenancy-owned audit ledger is written through the shared
    `record_audit_event` helper.
    """

    # ------------------------------------------------------------- red lines

    @staticmethod
    def _validate_pattern(pattern: str, match_type: str) -> str:
        """Reject unsafe or uncompilable regex red lines before they are stored."""
        trimmed = pattern.strip()
        if not trimmed:
            raise AppError(
                slug="empty-redline-pattern",
                status_code=422,
                code="ERR_EMPTY_REDLINE_PATTERN",
                title="Empty Red Line Pattern",
                detail="A red-line rule needs a non-empty pattern.",
            )
        if match_type == "regex":
            if len(trimmed) > _MAX_PATTERN_LENGTH:
                raise _invalid_regex(trimmed)
            try:
                re.compile(trimmed)
            except re.error as exc:
                raise _invalid_regex(trimmed) from exc
        elif len(trimmed) > _MAX_PATTERN_LENGTH:
            raise AppError(
                slug="redline-pattern-too-long",
                status_code=422,
                code="ERR_REDLINE_PATTERN_TOO_LONG",
                title="Red Line Pattern Too Long",
                detail=f"Patterns are capped at {_MAX_PATTERN_LENGTH} characters.",
            )
        return trimmed

    async def create_redline_rule(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: RedlineRuleCreate,
        *,
        actor_id: str,
    ) -> RedlineRuleRead:
        pattern = self._validate_pattern(payload.pattern, payload.match_type)
        rule_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO outbound_redline_rules "
                "(id, tenant_id, name, pattern, match_type, action, is_enabled) "
                "VALUES (:id, :tid, :name, :pattern, :match_type, :action, :enabled)"
            ),
            {
                "id": rule_id,
                "tid": tenant_id,
                "name": payload.name.strip(),
                "pattern": pattern,
                "match_type": payload.match_type,
                "action": payload.action,
                "enabled": payload.is_enabled,
            },
        )
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "ai.redline_rule.created",
            "outbound_redline_rule",
            rule_id,
            after_state={
                "name": payload.name.strip(),
                "match_type": payload.match_type,
                "action": payload.action,
            },
        )
        row = (
            await session.execute(
                text(f"{_RULE_COLUMNS} WHERE tenant_id = :tid AND id = :rid"),
                {"tid": tenant_id, "rid": rule_id},
            )
        ).one()
        return _rule_from_row(row)

    async def list_redline_rules(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[RedlineRuleRead]:
        rows = (
            await session.execute(
                text(f"{_RULE_COLUMNS} WHERE tenant_id = :tid ORDER BY created_at DESC, id"),
                {"tid": tenant_id},
            )
        ).all()
        return [_rule_from_row(row) for row in rows]

    async def delete_redline_rule(
        self, session: AsyncSession, tenant_id: UUID, rule_id: UUID, *, actor_id: str
    ) -> None:
        deleted = await session.execute(
            text(
                "DELETE FROM outbound_redline_rules WHERE tenant_id = :tid AND id = :rid "
                "RETURNING name, pattern, action"
            ),
            {"tid": tenant_id, "rid": rule_id},
        )
        removed = deleted.first()
        if removed is None:
            raise _rule_not_found(rule_id)
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "ai.redline_rule.deleted",
            "outbound_redline_rule",
            rule_id,
            before_state={
                "name": str(removed.name),
                "pattern": str(removed.pattern),
                "action": str(removed.action),
            },
        )

    async def check_outbound_text(
        self, session: AsyncSession, tenant_id: UUID, content: str
    ) -> RedlineCheckResult:
        rows = (
            await session.execute(
                text(
                    f"{_RULE_COLUMNS} WHERE tenant_id = :tid AND is_enabled = true "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"tid": tenant_id},
            )
        ).all()

        blocks: list[str] = []
        warnings: list[str] = []
        matched: list[RedlineMatchedRule] = []
        for row in rows:
            rule = _rule_from_row(row)
            matched_text = self._match(rule.pattern, rule.match_type, content)
            if matched_text is None:
                continue
            matched.append(
                RedlineMatchedRule(
                    id=rule.id,
                    name=rule.name,
                    match_type=rule.match_type,
                    action=rule.action,
                    matched_text=matched_text,
                )
            )
            bucket = blocks if rule.action == "block" else warnings
            if matched_text not in bucket:
                bucket.append(matched_text)

        return RedlineCheckResult(
            blocked=len(blocks) > 0,
            blocks=blocks,
            warnings=warnings,
            matched_rules=matched,
        )

    @staticmethod
    def _match(pattern: str, match_type: str, content: str) -> str | None:
        if match_type == "keyword":
            if pattern.casefold() in content.casefold():
                return pattern
            return None
        try:
            found = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        except re.error:
            return None
        if found is None:
            return None
        return found.group(0)[:200]

    # -------------------------------------------------------------- KB gaps

    async def list_kb_gaps(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResponse[KbGapRead]:
        clauses = ["tenant_id = :tid"]
        params: dict[str, object] = {"tid": tenant_id, "lim": limit, "off": offset}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status
        where_sql = " AND ".join(clauses)

        total = int(
            await session.scalar(
                text(f"SELECT COUNT(*) FROM kb_gap_items WHERE {where_sql}"), params
            )
            or 0
        )
        rows = (
            await session.execute(
                text(
                    f"{_GAP_COLUMNS} WHERE {where_sql} "
                    "ORDER BY frequency_count DESC, updated_at DESC, id "
                    "LIMIT :lim OFFSET :off"
                ),
                params,
            )
        ).all()
        return PaginatedResponse.build(
            items=[_gap_from_row(row) for row in rows], total=total, limit=limit, offset=offset
        )

    async def create_or_bump_kb_gap(
        self, session: AsyncSession, tenant_id: UUID, payload: KbGapCreateRequest
    ) -> KbGapRead:
        question = payload.customer_question.strip()
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO kb_gap_items (
                        tenant_id, conversation_id, customer_question, detected_language
                    ) VALUES (:tid, :cid, :question, :lang)
                    ON CONFLICT (tenant_id, customer_question) WHERE status = 'open'
                    DO UPDATE SET
                        frequency_count = kb_gap_items.frequency_count + 1,
                        updated_at = now()
                    RETURNING id
                    """
                ),
                {
                    "tid": tenant_id,
                    "cid": payload.conversation_id,
                    "question": question,
                    "lang": payload.detected_language,
                },
            )
        ).one()
        gap_row = (
            await session.execute(
                text(f"{_GAP_COLUMNS} WHERE tenant_id = :tid AND id = :gid"),
                {"tid": tenant_id, "gid": row.id},
            )
        ).one()
        return _gap_from_row(gap_row)

    async def dismiss_kb_gap(
        self, session: AsyncSession, tenant_id: UUID, gap_id: UUID, *, actor_id: str
    ) -> KbGapRead:
        updated = (
            await session.execute(
                text(
                    "UPDATE kb_gap_items SET status = 'dismissed', updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :gid AND status = 'open' "
                    "RETURNING id"
                ),
                {"tid": tenant_id, "gid": gap_id},
            )
        ).first()
        if updated is None:
            existing = await session.scalar(
                text("SELECT status FROM kb_gap_items WHERE tenant_id = :tid AND id = :gid"),
                {"tid": tenant_id, "gid": gap_id},
            )
            if existing is None:
                raise _gap_not_found(gap_id)
            raise AppError(
                slug="kb-gap-not-open",
                status_code=409,
                code="ERR_KB_GAP_NOT_OPEN",
                title="Knowledge Base Gap Not Open",
                detail=f"Gap '{gap_id}' is '{existing}' and can no longer be dismissed.",
            )

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "ai.kb_gap.dismissed",
            "kb_gap_item",
            gap_id,
            after_state={"status": "dismissed"},
        )
        gap_row = (
            await session.execute(
                text(f"{_GAP_COLUMNS} WHERE tenant_id = :tid AND id = :gid"),
                {"tid": tenant_id, "gid": gap_id},
            )
        ).one()
        return _gap_from_row(gap_row)

    async def promote_kb_gap(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        gap_id: UUID,
        resolved_answer: str,
        *,
        actor_id: str,
    ) -> KbGapPromoteResult:
        """Turn a gap into a real searchable `kb_documents` + `kb_chunks` row."""
        gap_row = (
            await session.execute(
                text(f"{_GAP_COLUMNS} WHERE tenant_id = :tid AND id = :gid"),
                {"tid": tenant_id, "gid": gap_id},
            )
        ).first()
        if gap_row is None:
            raise _gap_not_found(gap_id)
        gap = _gap_from_row(gap_row)
        if gap.status == "dismissed":
            raise AppError(
                slug="kb-gap-dismissed",
                status_code=409,
                code="ERR_KB_GAP_DISMISSED",
                title="Knowledge Base Gap Dismissed",
                detail="A dismissed gap cannot be promoted; reopen it first.",
            )

        answer = resolved_answer.strip()
        raw_content = f"Q: {gap.customer_question}\nA: {answer}"
        content = strip_dynamic_prices_for_answer(raw_content)
        flagged = has_dynamic_pricing_signals(raw_content)

        embedding = DeterministicHashEmbeddingProvider().embed_one(content)
        vector_literal = format_pgvector_literal(embedding)
        token_count = len(_TOKEN_RE.findall(content))

        doc_id = uuid4()
        title = f"KB gap: {gap.customer_question}"[:120]
        doc_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

        await session.execute(
            text(
                "INSERT INTO kb_documents (id, tenant_id, title, source_type, source_uri, "
                "doc_hash, language, is_active, metadata_json) "
                "VALUES (:id, :tid, :title, 'faq_json', :source_uri, :doc_hash, :lang, true, "
                "CAST(:meta AS jsonb))"
            ),
            {
                "id": doc_id,
                "tid": tenant_id,
                "title": title,
                "source_uri": f"kb-gap:{gap.id}",
                "doc_hash": doc_hash,
                "lang": gap.detected_language,
                "meta": json.dumps(
                    {
                        "origin": "kb_gap",
                        "gap_id": str(gap.id),
                        "frequency_count": gap.frequency_count,
                    }
                ),
            },
        )

        await session.execute(
            text(
                "INSERT INTO kb_chunks (id, tenant_id, document_id, chunk_index, content, "
                "embedding, has_dynamic_price_disclaimer, token_count) "
                "VALUES (:id, :tid, :doc_id, 0, :content, "
                "CAST(CAST(:emb AS text) AS vector), :flag, :tokens)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "doc_id": doc_id,
                "content": content,
                "emb": vector_literal,
                "flag": flagged,
                "tokens": token_count,
            },
        )

        await session.execute(
            text(
                "UPDATE kb_gap_items SET status = 'resolved', resolved_answer = :answer, "
                "promoted_kb_document_id = :doc_id, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :gid"
            ),
            {"answer": answer, "doc_id": doc_id, "tid": tenant_id, "gid": gap_id},
        )

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "ai.kb_gap.promoted",
            "kb_gap_item",
            gap_id,
            after_state={
                "status": "resolved",
                "promoted_kb_document_id": str(doc_id),
                "chunks_created": 1,
                "has_dynamic_price_disclaimer": flagged,
            },
        )

        refreshed = (
            await session.execute(
                text(f"{_GAP_COLUMNS} WHERE tenant_id = :tid AND id = :gid"),
                {"tid": tenant_id, "gid": gap_id},
            )
        ).one()
        return KbGapPromoteResult(
            gap=_gap_from_row(refreshed), document_id=doc_id, chunks_created=1
        )

    # ------------------------------------------------------ adoption metrics

    async def get_ai_adoption_metrics(
        self, session: AsyncSession, tenant_id: UUID, days: int = 30
    ) -> AiAdoptionMetrics:
        window = max(1, min(int(days), 365))
        rows = (
            await session.execute(
                text(
                    """
                    SELECT status, proposal_type, COUNT(*) AS cnt
                    FROM ai_proposals
                    WHERE tenant_id = :tid
                      AND created_at >= now() - (CAST(:days AS integer) * INTERVAL '1 day')
                    GROUP BY status, proposal_type
                    """
                ),
                {"tid": tenant_id, "days": window},
            )
        ).all()

        totals: dict[str, int] = dict.fromkeys(_STATUS_TOKENS, 0)
        per_type: dict[str, dict[str, int]] = {}
        for row in rows:
            status = str(row.status)
            count = int(row.cnt)
            if status in totals:
                totals[status] += count
            bucket = per_type.setdefault(str(row.proposal_type), dict.fromkeys(_STATUS_TOKENS, 0))
            if status in bucket:
                bucket[status] += count

        total = sum(totals.values())
        adopted = totals["approved"] + totals["edited_and_approved"]
        review_base = adopted + totals["rejected"]

        by_type = [
            AdoptionTypeMetrics(
                proposal_type=proposal_type,
                total=sum(statuses.values()),
                pending=statuses["pending"],
                approved=statuses["approved"],
                edited_and_approved=statuses["edited_and_approved"],
                rejected=statuses["rejected"],
                adoption_rate_pct=_pct(
                    statuses["approved"] + statuses["edited_and_approved"],
                    statuses["approved"] + statuses["edited_and_approved"] + statuses["rejected"],
                ),
            )
            for proposal_type, statuses in per_type.items()
        ]
        by_type.sort(key=lambda item: (-item.total, item.proposal_type))

        return AiAdoptionMetrics(
            tenant_id=tenant_id,
            window_days=window,
            total=total,
            pending=totals["pending"],
            approved=totals["approved"],
            edited_and_approved=totals["edited_and_approved"],
            rejected=totals["rejected"],
            expired=totals["expired"],
            failed=totals["failed"],
            adopted=adopted,
            review_base=review_base,
            adoption_rate_pct=_pct(adopted, review_base),
            direct_adopt_rate_pct=_pct(totals["approved"], review_base),
            edit_adopt_rate_pct=_pct(totals["edited_and_approved"], adopted),
            rejection_rate_pct=_pct(totals["rejected"], review_base),
            by_type=by_type,
        )
