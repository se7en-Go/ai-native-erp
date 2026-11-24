from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import TextClause, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
from erp_core.ai_engine.models import AiProposal
from erp_core.ai_engine.proposal_executor import (
    ProposalExecutorService,
    proposal_error,
    visible_proposal_types,
)
from erp_core.ai_engine.proposal_generator import AiProposalGeneratorService
from erp_core.ai_engine.runtime import PolicyRuntime, load_ai_policy
from erp_core.ai_engine.schemas import (
    AiProposalRead,
    AssistantChatRequest,
    AssistantChatResponse,
    CapabilitiesOverviewResponse,
    CapabilitiesUpdateRequest,
    CapabilityPolicyItem,
    ProposalEditApproveRequest,
    ProposalFeedbackRequest,
    ProposalFeedbackResponse,
    ProposalRejectRequest,
    TranslateRequest,
    TranslateResponse,
)
from erp_core.shared.enums import (
    AiCapability,
    AutonomyLevel,
    ProposalStatus,
    ProposalType,
    UserRole,
)
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])
Principal = Annotated[CurrentPrincipal, Depends(get_current_principal)]
PolicyEditor = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
Db = Annotated[AsyncSession, Depends(get_tenant_db)]
_CONTEXT_FIELDS = frozenset(
    {
        "action",
        "qty",
        "order_no",
        "items",
        "incoterm",
        "currency",
        "sql",
        "buyer_timezone",
        "amount",
        "reference_hint",
        "channel_metadata",
        "target_lang",
        "window",
        "channel_types",
    }
)


# ---------------------------------------------------------------------------------------
# AGENT personal-mailbox isolation for AI proposals (GPT #08).
#
# An AGENT may only read or review a proposal whose conversation they can work: either the
# proposal is not tied to a conversation at all, or the conversation is assigned to them, or
# it is unassigned on an unowned SHARED mailbox. This mirrors
# `channels.inbox_service.agent_can_access_conversation`, but stays plain tenant-scoped SQL
# so the modular-monolith import contracts (see infra/importlinter.ini) stay intact.
_AGENT_VISIBLE_PROPOSAL_SQL = """
    (
        ai_proposals.conversation_id IS NULL
        OR EXISTS (
            SELECT 1
            FROM conversations conv
            JOIN channel_accounts ca
              ON ca.id = conv.channel_account_id
             AND ca.tenant_id = conv.tenant_id
            WHERE conv.tenant_id = :visibility_tenant_id
              AND conv.id = ai_proposals.conversation_id
              AND (
                    conv.assigned_user_id = :visibility_user_id
                    OR (
                        conv.assigned_user_id IS NULL
                        AND ca.visibility_scope = 'shared'
                        AND ca.owner_user_id IS NULL
                    )
                  )
        )
    )
"""


def _agent_proposal_visibility_clause(*, tenant_id: UUID, user_id: UUID) -> TextClause:
    """Build the AGENT proposal visibility predicate bound to this tenant and salesperson."""
    return text(_AGENT_VISIBLE_PROPOSAL_SQL).bindparams(
        visibility_tenant_id=tenant_id, visibility_user_id=user_id
    )


async def _assert_proposal_visibility(
    session: AsyncSession, principal: CurrentPrincipal, proposal_id: UUID
) -> None:
    """Hide a proposal from an AGENT when its conversation sits in another private mailbox.

    A 404 (not 403) is returned so the endpoint never confirms the existence of a
    proposal belonging to a colleague's personal mailbox.
    """
    if principal.role != UserRole.AGENT:
        return
    visible = await session.scalar(
        select(AiProposal.id).where(
            AiProposal.tenant_id == principal.tenant_id,
            AiProposal.id == proposal_id,
            _agent_proposal_visibility_clause(
                tenant_id=principal.tenant_id, user_id=principal.user_id
            ),
        )
    )
    if visible is None:
        raise proposal_error(404, "ERR_PROPOSAL_NOT_FOUND", "Proposal not found.")


def _executor(request: Request) -> ProposalExecutorService:
    return ProposalExecutorService(redis=request.app.state.redis)


def _runtime(request: Request) -> PolicyRuntime:
    runtime: PolicyRuntime = request.app.state.ai_runtime
    return runtime


Executor = Annotated[ProposalExecutorService, Depends(_executor)]
Runtime = Annotated[PolicyRuntime, Depends(_runtime)]


def _proposal_generator(request: Request) -> AiProposalGeneratorService:
    """Factory seam so tests can inject a deterministic offline LLM client."""
    return AiProposalGeneratorService()


@router.post("/conversations/{conversation_id}/propose-reply")
async def propose_conversation_reply(
    conversation_id: UUID,
    request: Request,
    session: Db,
    principal: Annotated[
        CurrentPrincipal,
        Depends(
            require_roles(
                UserRole.OWNER,
                UserRole.ADMIN,
                UserRole.MANAGER,
                UserRole.AGENT,
            )
        ),
    ],
) -> AiProposalRead:
    """On-demand grounded reply draft backed by live ERP SQL tools."""
    service = _proposal_generator(request)
    conversation = await service.load_conversation(
        session, principal.tenant_id, conversation_id
    )
    service.assert_visibility(principal, conversation)
    return await service.generate_reply_proposal(
        session,
        principal.tenant_id,
        conversation_id,
        redis=getattr(request.app.state, "redis", None),
    )


@router.get("/proposals")
async def list_proposals(
    session: Db,
    principal: Principal,
    status: ProposalStatus | None = None,
    proposal_type: ProposalType | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AiProposalRead]:
    filters = [
        AiProposal.tenant_id == principal.tenant_id,
        AiProposal.proposal_type.in_(visible_proposal_types(principal.role)),
    ]
    if status is not None:
        filters.append(AiProposal.status == status.value)
    if proposal_type is not None:
        filters.append(AiProposal.proposal_type == proposal_type.value)
    query = select(AiProposal).where(*filters)
    count_query = select(func.count()).select_from(AiProposal).where(*filters)
    if principal.role == UserRole.AGENT:
        visibility = _agent_proposal_visibility_clause(
            tenant_id=principal.tenant_id, user_id=principal.user_id
        )
        query = query.where(visibility)
        count_query = count_query.where(visibility)
    rows = (
        await session.scalars(
            query.order_by(
                AiProposal.created_at.desc(),
                AiProposal.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(count_query)
    return PaginatedResponse.build(
        items=[AiProposalRead.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/proposals/{proposal_id}")
async def proposal_detail(
    proposal_id: UUID, session: Db, principal: Principal, executor: Executor
) -> AiProposalRead:
    await _assert_proposal_visibility(session, principal, proposal_id)
    return await executor.get(session, principal, proposal_id)


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: UUID, session: Db, principal: Principal, executor: Executor
) -> AiProposalRead:
    await _assert_proposal_visibility(session, principal, proposal_id)
    return await executor.approve(session, principal, proposal_id)


@router.post("/proposals/{proposal_id}/edit-approve")
async def edit_approve_proposal(
    proposal_id: UUID,
    body: ProposalEditApproveRequest,
    session: Db,
    principal: Principal,
    executor: Executor,
) -> AiProposalRead:
    await _assert_proposal_visibility(session, principal, proposal_id)
    return await executor.edit_approve(
        session, principal, proposal_id, body.edited_payload, body.feedback_note
    )


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: UUID,
    body: ProposalRejectRequest,
    session: Db,
    principal: Principal,
    executor: Executor,
) -> AiProposalRead:
    await _assert_proposal_visibility(session, principal, proposal_id)
    return await executor.reject(session, principal, proposal_id, body.reason)


@router.post("/feedback")
async def feedback(
    body: ProposalFeedbackRequest, session: Db, principal: Principal, executor: Executor
) -> ProposalFeedbackResponse:
    await _assert_proposal_visibility(session, principal, body.proposal_id)
    fid = await executor.add_feedback(session, principal, body.proposal_id, body.feedback_note)
    return ProposalFeedbackResponse(proposal_id=body.proposal_id, feedback_id=fid)


@router.get("/capabilities")
async def get_capabilities(session: Db, principal: Principal) -> CapabilitiesOverviewResponse:
    policy = await load_ai_policy(session, principal.tenant_id) or {}
    cutoff = datetime.now(UTC) - timedelta(days=7)
    count = func.count()
    rows = (
        await session.execute(
            select(
                AiProposal.capability,
                count.filter(
                    AiProposal.status == ProposalStatus.PENDING, AiProposal.created_at >= cutoff
                ),
                count.filter(
                    AiProposal.status.in_(
                        [ProposalStatus.APPROVED, ProposalStatus.EDITED_AND_APPROVED]
                    ),
                    AiProposal.reviewed_at >= cutoff,
                ),
            )
            .where(
                AiProposal.tenant_id == principal.tenant_id,
                AiProposal.proposal_type.in_(visible_proposal_types(principal.role)),
            )
            .group_by(AiProposal.capability)
        )
    ).all()
    counts = {row[0]: (row[1], row[2]) for row in rows}
    capabilities = []
    for cap, spec in CAPABILITY_SPECS.items():
        try:
            configured = AutonomyLevel(policy.get(cap.value, spec.default_autonomy))
        except (TypeError, ValueError):
            configured = AutonomyLevel.L0_COPILOT
        pending, executed = counts.get(cap.value, (0, 0))
        capabilities.append(
            CapabilityPolicyItem(
                capability=cap,
                display_name=spec.display_name,
                configured_autonomy=min_autonomy(configured, spec.max_allowed_autonomy),
                max_allowed_autonomy=spec.max_allowed_autonomy,
                pending_proposals_7d=pending,
                executed_actions_7d=executed,
            )
        )
    return CapabilitiesOverviewResponse(capabilities=capabilities)


@router.patch("/capabilities")
async def update_capabilities(
    body: CapabilitiesUpdateRequest, session: Db, principal: PolicyEditor
) -> CapabilitiesOverviewResponse:
    for cap, level in body.settings.items():
        if min_autonomy(level, CAPABILITY_SPECS[cap].max_allowed_autonomy) != level:
            raise proposal_error(
                422, "ERR_CAPABILITY_CEILING", f"{cap.value} cannot run at {level.value}."
            )
    tid = principal.tenant_id
    old = await load_ai_policy(session, tid, lock=True) or {}
    changes = {cap.value: level.value for cap, level in body.settings.items()}
    merged = {**old, **changes}
    if merged != old:
        await session.execute(
            text("UPDATE tenants SET ai_policy_settings = CAST(:policy AS jsonb) WHERE id = :tid"),
            {"tid": tid, "policy": json.dumps(merged)},
        )
        await record_audit_event(
            session,
            tid,
            "human_user",
            str(principal.user_id),
            "ai.capabilities.updated",
            "tenant",
            tid,
            before_state={key: old.get(key) for key in changes},
            after_state=changes,
        )
    return await get_capabilities(session, principal)


def _check_capability(role: UserRole, capability: AiCapability) -> None:
    if (
        capability in {AiCapability.BIZ_ANALYTICS, AiCapability.SALES_QUOTING}
        and role == UserRole.AGENT
    ):
        raise proposal_error(
            403, "ERR_INSUFFICIENT_ROLE", "Capability requires access to commercial data."
        )
    if capability == AiCapability.FINANCE_RECON and role not in {UserRole.OWNER, UserRole.FINANCE}:
        raise proposal_error(
            403, "ERR_INSUFFICIENT_ROLE", "Capability requires finance permissions."
        )
    if capability == AiCapability.SALES_QUOTING and role == UserRole.FINANCE:
        raise proposal_error(403, "ERR_INSUFFICIENT_ROLE", "Capability requires sales permissions.")


@router.post("/assistant/chat")
async def assistant_chat(
    body: AssistantChatRequest, session: Db, principal: Principal, runtime: Runtime
) -> AssistantChatResponse:
    _check_capability(principal.role, body.capability)
    payload = {key: val for key, val in body.context.items() if key in _CONTEXT_FIELDS}
    payload.update(
        message=body.message, conversation_id=body.conversation_id, channel_type=body.channel_type
    )
    try:
        result = await runtime.run_capability(
            session, principal.tenant_id, body.capability, payload, body.strict_rag
        )
    except RuntimeError:
        raise proposal_error(
            503, "ERR_AI_PROVIDER_UNAVAILABLE", "AI provider unavailable."
        ) from None
    if result.reason == "conversation_not_found":
        raise proposal_error(404, "ERR_CONVERSATION_NOT_FOUND", "Conversation not found.")
    return AssistantChatResponse.model_validate(result.model_dump())


@router.post("/translate")
async def translate_text(
    body: TranslateRequest, session: Db, principal: Principal, runtime: Runtime
) -> TranslateResponse:
    try:
        result = await runtime.run_capability(
            session,
            principal.tenant_id,
            AiCapability.CONTENT_STUDIO,
            {"message": body.text, "target_lang": body.target_lang, "action": "translate"},
        )
    except RuntimeError:
        raise proposal_error(
            503, "ERR_AI_PROVIDER_UNAVAILABLE", "AI provider unavailable."
        ) from None
    if result.degraded_to_l0:
        raise proposal_error(402, "ERR_CREDITS_DEPLETED", "Top up to resume AI translation.")
    if result.l0_locked:
        raise proposal_error(409, "ERR_MANUAL_ACTION_REQUIRED", "Translation is set to manual.")
    return TranslateResponse(
        translated_text=result.reply_text,
        target_lang=body.target_lang,
        model_used=result.model_used,
        credits_consumed=result.credits_consumed,
    )
