from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.compliance_filter import OutboundComplianceFilter
from erp_core.ai_engine.ingestion import DeterministicHashEmbeddingProvider
from erp_core.ai_engine.models import AiEvalFeedback, AiProposal
from erp_core.ai_engine.runtime import load_ai_policy
from erp_core.ai_engine.schemas import AiProposalRead
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.channels.inbox_schemas import SendInboxMessageRequest
from erp_core.channels.inbox_service import InboxService
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import LeadCreate, OpportunityCreate, QuoteCreate, QuoteItemInput
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.enums import Incoterm, ProposalStatus, ProposalType, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

_COMMERCIAL = {UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER}
_CUSTOMER_SERVICE = _COMMERCIAL | {UserRole.AGENT}
_REVIEWERS = {
    ProposalType.REPLY_DRAFT: _CUSTOMER_SERVICE,
    ProposalType.FOLLOW_UP_SUGGESTION: _CUSTOMER_SERVICE,
    ProposalType.QUOTE_DRAFT: _COMMERCIAL,
    ProposalType.CRM_ENTITY_EXTRACT: _COMMERCIAL,
    ProposalType.IDENTITY_MERGE: _COMMERCIAL,
    ProposalType.REFUND_OR_RESHIP: _COMMERCIAL,
    ProposalType.PAYMENT_MATCH: {UserRole.OWNER, UserRole.FINANCE},
}
_L0_ONLY = {ProposalType.PAYMENT_MATCH, ProposalType.REFUND_OR_RESHIP}
_LIVE_REPLY = re.compile(
    r"[$€£]|\b(?:unit price|in stock|tracking|moq|refund|formal quote|quotation|bank slip)\b|"
    r"ORD-[A-Z0-9-]+|退款|报价|水单|单号|库存",
    re.I,
)


def proposal_error(status: int, code: str, detail: str) -> AppError:
    return AppError(
        slug=code.removeprefix("ERR_").lower().replace("_", "-"),
        title=code.removeprefix("ERR_").replace("_", " ").title(),
        status_code=status,
        detail=detail,
        code=code,
    )


def visible_proposal_types(role: UserRole) -> list[str]:
    return [kind.value for kind, roles in _REVIEWERS.items() if role in roles]


def compute_structured_diff(original: dict[str, Any], edited: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": {k: edited[k] for k in sorted(edited.keys() - original.keys())},
        "removed": {k: original[k] for k in sorted(original.keys() - edited.keys())},
        "changed": {
            k: {"before": original[k], "after": edited[k]}
            for k in sorted(original.keys() & edited.keys())
            if original[k] != edited[k]
        },
    }


class ProposalExecutorService:
    def __init__(self, redis: Redis | None = None) -> None:
        self.redis = redis
        self.quotes = QuoteService()
        self.tools = ErpToolRegistry()
        self.compliance = OutboundComplianceFilter()

    async def _get(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
        *,
        lock: bool = False,
    ) -> AiProposal:
        query = select(AiProposal).where(
            AiProposal.id == proposal_id,
            AiProposal.tenant_id == principal.tenant_id,
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise proposal_error(404, "ERR_PROPOSAL_NOT_FOUND", "Proposal not found.")
        if principal.role not in _REVIEWERS[ProposalType(row.proposal_type)]:
            raise proposal_error(403, "ERR_INSUFFICIENT_ROLE", "Role cannot access this proposal.")
        return row

    async def get(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
    ) -> AiProposalRead:
        return AiProposalRead.model_validate(await self._get(session, principal, proposal_id))

    async def approve(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
    ) -> AiProposalRead:
        return await self._review(session, principal, proposal_id, ProposalStatus.APPROVED)

    async def edit_approve(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
        edited_payload: dict[str, Any],
        feedback_note: str | None = None,
    ) -> AiProposalRead:
        return await self._review(
            session,
            principal,
            proposal_id,
            ProposalStatus.EDITED_AND_APPROVED,
            edited=edited_payload,
            feedback_note=feedback_note,
        )

    async def reject(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
        reason: str,
    ) -> AiProposalRead:
        return await self._review(
            session, principal, proposal_id, ProposalStatus.REJECTED, reason=reason
        )

    async def _review(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
        requested: ProposalStatus,
        *,
        edited: dict[str, Any] | None = None,
        reason: str | None = None,
        feedback_note: str | None = None,
    ) -> AiProposalRead:
        row = await self._get(session, principal, proposal_id, lock=True)
        if (
            row.status == requested
            and row.edited_payload == edited
            and row.rejection_reason == reason
        ):
            return AiProposalRead.model_validate(row)
        if row.status != ProposalStatus.PENDING:
            raise proposal_error(
                409, "ERR_PROPOSAL_ALREADY_REVIEWED", "Proposal is no longer pending."
            )
        now = datetime.now(UTC)
        terminal = requested
        execution: dict[str, Any] | None = None
        if row.expires_at and row.expires_at <= now:
            terminal = ProposalStatus.EXPIRED
        elif requested != ProposalStatus.REJECTED:
            if ProposalType(row.proposal_type) in _L0_ONLY:
                raise proposal_error(
                    409,
                    "ERR_MANUAL_ACTION_REQUIRED",
                    "Use the dedicated manual workflow for this action.",
                )
            try:
                async with session.begin_nested():
                    execution = await self._execute(
                        session,
                        principal,
                        row,
                        edited if edited is not None else row.proposed_payload,
                    )
            except (
                ValidationError,
                KeyError,
                ValueError,
                TypeError,
                InvalidOperation,
                LookupError,
            ):
                raise proposal_error(
                    422, "ERR_INVALID_PROPOSAL_PAYLOAD", "A valid reviewed payload is required."
                ) from None
            except AppError as exc:
                if exc.code not in {
                    "ERR_PROPOSAL_STALE",
                    "ERR_MARGIN_FLOOR_VIOLATION",
                    "ERR_PROPOSAL_COMPLIANCE_CHANGED",
                }:
                    raise
                terminal = ProposalStatus.FAILED
                execution = {"code": exc.code, "detail": exc.detail}
        row.status = terminal.value
        row.reviewed_by_user_id = principal.user_id
        row.reviewed_at = now
        row.execution_result = execution
        row.edited_payload = edited
        row.rejection_reason = reason
        await session.flush()
        if edited is not None and terminal in {
            ProposalStatus.EDITED_AND_APPROVED,
            ProposalStatus.FAILED,
        }:
            await self._feedback(session, principal, row, feedback_note)
        await self._audit(session, principal, row, terminal.value, {"status": "pending"})
        return AiProposalRead.model_validate(row)

    @staticmethod
    async def _audit(
        session: AsyncSession,
        principal: CurrentPrincipal,
        row: AiProposal,
        suffix: str,
        before_state: dict[str, Any] | None = None,
    ) -> None:
        await record_audit_event(
            session,
            principal.tenant_id,
            "human_user",
            str(principal.user_id),
            f"ai.proposal.{suffix}",
            "ai_proposal",
            row.id,
            proposal_id=row.id,
            before_state=before_state,
            after_state={"status": row.status, "execution_result": row.execution_result},
        )

    async def add_feedback(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        proposal_id: UUID,
        note: str,
    ) -> UUID:
        row = await self._get(session, principal, proposal_id, lock=True)
        if row.status == ProposalStatus.PENDING:
            raise proposal_error(409, "ERR_PROPOSAL_NOT_REVIEWED", "Review the proposal first.")
        feedback_id = await self._feedback(session, principal, row, note)
        await self._audit(session, principal, row, "feedback")
        return feedback_id

    @staticmethod
    async def _feedback(
        session: AsyncSession,
        principal: CurrentPrincipal,
        row: AiProposal,
        note: str | None,
    ) -> UUID:
        edited = row.edited_payload if row.edited_payload is not None else row.proposed_payload
        (embedding,) = await DeterministicHashEmbeddingProvider().embed_texts(
            [json.dumps(edited, sort_keys=True) + ("\n" + note if note else "")],
        )
        stmt = insert(AiEvalFeedback).values(
            tenant_id=principal.tenant_id,
            proposal_id=row.id,
            capability=row.capability,
            proposal_type=row.proposal_type,
            original_payload=row.proposed_payload,
            edited_payload=edited,
            diff_patch=compute_structured_diff(row.proposed_payload, edited),
            feedback_note=note,
            embedding=embedding,
            created_by_user_id=principal.user_id,
        )
        return (
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[AiEvalFeedback.tenant_id, AiEvalFeedback.proposal_id],
                    set_={"feedback_note": note, "embedding": embedding},
                ).returning(AiEvalFeedback.id)
            )
        ).scalar_one()

    async def _execute(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        row: AiProposal,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tid = principal.tenant_id
        kind = ProposalType(row.proposal_type)
        conversation = (
            await self.compliance.load_conversation(session, tid, row.conversation_id)
            if row.conversation_id
            else None
        )
        if row.conversation_id and conversation is None:
            raise proposal_error(409, "ERR_PROPOSAL_STALE", "Conversation no longer exists.")
        if kind == ProposalType.IDENTITY_MERGE:
            event = await IdentityResolverService().merge_customers(
                session,
                tid,
                UUID(str(payload["source_customer_id"])),
                UUID(str(payload["target_customer_id"])),
                str(principal.user_id),
                str(payload.get("reason", "Human-approved identity merge")),
            )
            return {"merge_event_id": str(event)}
        if kind in {ProposalType.REPLY_DRAFT, ProposalType.FOLLOW_UP_SUGGESTION}:
            body = payload.get("body_text", payload.get("draft_message"))
            if (
                not conversation
                or not isinstance(body, str)
                or not body.strip()
                or len(body) > 10000
            ):
                raise ValueError("A conversation and nonempty body are required.")
            if self.redis is None:
                raise proposal_error(503, "ERR_CHANNEL_UNAVAILABLE", "Inbox is unavailable.")
            policy = await load_ai_policy(session, tid)
            guards = {"blocked_terms": (policy or {}).get("outbound_blocked_terms", [])}
            decision = self.compliance.check_outbound(
                conversation.channel_type, body, channel_metadata=guards
            )
            if decision.allowed:
                decision = await self.compliance.check_context(session, tid, conversation, {})
            if not decision.allowed:
                raise proposal_error(
                    409,
                    "ERR_PROPOSAL_COMPLIANCE_CHANGED",
                    decision.blocked_reason or "Outbound policy blocked the draft.",
                )
            if kind == ProposalType.REPLY_DRAFT and _LIVE_REPLY.search(body):
                raise proposal_error(
                    409,
                    "ERR_PROPOSAL_STALE",
                    "Verify live or sensitive claims through the manual inbox workflow.",
                )
            follow_up = kind == ProposalType.FOLLOW_UP_SUGGESTION
            schedule = payload.get("scheduled_at") if follow_up else None
            if follow_up:
                schedule = datetime.fromisoformat(str(schedule)).astimezone(UTC).isoformat()
            sent = await InboxService().send_message(
                session,
                self.redis,
                tid,
                principal,
                conversation.id,
                SendInboxMessageRequest(
                    direction="internal_note" if follow_up else "outbound",
                    body_text=f"Approved follow-up for {schedule}: {body}" if follow_up else body,
                ),
            )
            return {
                "message_id": str(sent.message.id),
                "scheduled_at": schedule,
                "queued_for_delivery": sent.queued_for_delivery,
            }

        customer_id = (
            conversation.customer_id if conversation else UUID(str(payload["customer_id"]))
        )
        customer = await session.scalar(
            text("""
                SELECT id FROM customers
                WHERE tenant_id = :tid AND id = :cid AND merged_into_customer_id IS NULL
            """),
            {"tid": tid, "cid": customer_id},
        )
        if customer is None:
            raise proposal_error(409, "ERR_PROPOSAL_STALE", "Customer is no longer available.")
        if kind == ProposalType.CRM_ENTITY_EXTRACT:
            requirements = payload.get(
                "extracted_requirements", {"inquiry": payload.get("requirements", "")}
            )
            lead = await self.quotes.create_lead(
                session,
                tid,
                LeadCreate(
                    customer_id=customer,
                    source_channel=conversation.channel_type.value
                    if conversation
                    else str(payload.get("source_channel", "email")),
                    extracted_requirements=requirements,
                ),
            )
            return {"lead_id": str(lead.id), "customer_id": str(customer)}
        return await self._create_quote(session, principal, row, payload, customer)

    async def _create_quote(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        row: AiProposal,
        payload: dict[str, Any],
        customer_id: UUID,
    ) -> dict[str, Any]:
        tid = principal.tenant_id
        currency = str(payload.get("quote_currency", "USD")).upper()
        incoterm = Incoterm(payload.get("incoterm", "FOB"))
        items = payload["items"]
        if not isinstance(items, list) or not 0 < len(items) <= 100:
            raise ValueError("Expected between 1 and 100 items.")
        matrix = await self.tools.calc_b2b_quote_matrix(session, tid, items, currency, incoterm)
        if not matrix.ok or matrix.data["below_margin_floor"]:
            raise proposal_error(
                409, "ERR_PROPOSAL_STALE", "Pricing or margin policy requires a new review."
            )
        current = {k: v for k, v in matrix.data.items() if k != "source_tool_call_id"}
        if payload is row.proposed_payload:
            saved = row.proposed_payload.get("quote_matrix", {})
            expected = {k: v for k, v in saved.items() if k != "source_tool_call_id"}
            if expected and current != expected:
                raise proposal_error(
                    409, "ERR_PROPOSAL_STALE", "Quote pricing, FX or terms changed."
                )
        if payload.get("require_in_stock"):
            requested: dict[str, int] = {}
            for line in current["items"]:
                requested[line["sku"]] = requested.get(line["sku"], 0) + line["quantity"]
            for sku, qty in sorted(requested.items()):
                live = await self.tools.get_live_sku_pricing_and_stock(session, tid, sku, qty)
                if not live.ok or live.data["available_qty"] < qty:
                    raise proposal_error(
                        409, "ERR_PROPOSAL_STALE", "Requested inventory is no longer available."
                    )
        if raw_opp := payload.get("opportunity_id"):
            opportunity_id = UUID(str(raw_opp))
            if not await session.scalar(
                text("""
                    SELECT id FROM opportunities
                    WHERE tenant_id = :tid AND id = :oid AND customer_id = :cid
                """),
                {"tid": tid, "oid": opportunity_id, "cid": customer_id},
            ):
                raise proposal_error(
                    409, "ERR_PROPOSAL_STALE", "Opportunity no longer matches the buyer."
                )
        else:
            opportunity_id = (
                await self.quotes.create_opportunity(
                    session,
                    tid,
                    OpportunityCreate(
                        customer_id=customer_id,
                        title="Reviewed AI quote draft",
                        currency=currency,
                        expected_amount=Decimal(current["total_amount"]),
                        owner_user_id=principal.user_id,
                    ),
                )
            ).id
        quote = await self.quotes.create_quote(
            session,
            tid,
            principal,
            QuoteCreate(
                opportunity_id=opportunity_id,
                incoterm=incoterm,
                base_currency=current["base_currency"],
                quote_currency=currency,
                fx_rate_snapshot=Decimal(current["fx_rate_snapshot"]),
                fx_locked_until=datetime.now(UTC) + timedelta(days=7),
                items=[
                    QuoteItemInput(
                        variant_id=line["variant_id"],
                        quantity=line["quantity"],
                        unit_price_quote_ccy=Decimal(line["unit_price_quote_ccy"]),
                    )
                    for line in current["items"]
                ],
            ),
        )
        return {"quote_id": str(quote.id), "quote_no": quote.quote_no, "status": quote.status.value}
