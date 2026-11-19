from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
from erp_core.ai_engine.models import AiProposal
from erp_core.ai_engine.proposal_executor import (
    ProposalExecutorService,
    proposal_error,
    visible_proposal_types,
)
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
    }
)


def _executor(request: Request) -> ProposalExecutorService:
    return ProposalExecutorService(redis=request.app.state.redis)


def _runtime(request: Request) -> PolicyRuntime:
    runtime: PolicyRuntime = request.app.state.ai_runtime
    return runtime


Executor = Annotated[ProposalExecutorService, Depends(_executor)]
Runtime = Annotated[PolicyRuntime, Depends(_runtime)]


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
    rows = (
        await session.scalars(
            select(AiProposal)
            .where(*filters)
            .order_by(
                AiProposal.created_at.desc(),
                AiProposal.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(AiProposal).where(*filters))
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
    return await executor.get(session, principal, proposal_id)


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: UUID, session: Db, principal: Principal, executor: Executor
) -> AiProposalRead:
    return await executor.approve(session, principal, proposal_id)


@router.post("/proposals/{proposal_id}/edit-approve")
async def edit_approve_proposal(
    proposal_id: UUID,
    body: ProposalEditApproveRequest,
    session: Db,
    principal: Principal,
    executor: Executor,
) -> AiProposalRead:
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
    return await executor.reject(session, principal, proposal_id, body.reason)


@router.post("/feedback")
async def feedback(
    body: ProposalFeedbackRequest, session: Db, principal: Principal, executor: Executor
) -> ProposalFeedbackResponse:
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
