from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from erp_core.ai_engine.proposal_executor import proposal_error
from erp_core.ai_engine.router import Db, Runtime
from erp_core.ai_engine.runtime import CapabilityExecutionResult
from erp_core.shared.enums import AiCapability, ProposalType, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])
SandboxOwner = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
Scenario = Literal["b2c_wismo", "strict_rag_warranty", "b2b_moq_quote"]


class SandboxRequest(BaseModel):
    scenario_id: Scenario
    message: str = Field(min_length=1, max_length=2000)


class SandboxResult(BaseModel):
    passed: bool
    reply_text: str = ""
    source_tool_call_id: str | None = None


def _tool_evidence(result: CapabilityExecutionResult, name: str) -> str | None:
    for call in result.tool_calls:
        trace = call.get("tool_call_id")
        if call.get("tool_name") == name and call.get("ok") is True and isinstance(trace, str):
            return trace
    return None


@router.post("/sandbox-test")
async def run_sandbox_test(
    body: SandboxRequest,
    session: Db,
    principal: SandboxOwner,
    runtime: Runtime,
) -> SandboxResult:
    """Run the normal policy runtime; never use generated prose alone as proof."""
    query = body.message
    tid = principal.tenant_id
    try:
        if body.scenario_id == "strict_rag_warranty":
            answer = await runtime.run_capability(
                session,
                tid,
                AiCapability.CS_SUPPORT,
                {"message": query},
                strict_rag=True,
            )
            passed = bool(answer.reply_text and answer.citations and answer.numeric_guard_passed)
            return SandboxResult(passed=passed, reply_text=answer.reply_text if passed else "")
        if body.scenario_id == "b2c_wismo":
            order = re.search(r"#?[A-Z]{2,}[A-Z0-9]*-[A-Z0-9-]*\d[A-Z0-9-]*", query, re.I)
            if order is None:
                return SandboxResult(passed=False)
            answer = await runtime.run_capability(
                session,
                tid,
                AiCapability.ORDER_LOGISTICS,
                {
                    "message": query,
                    "order_no": order.group().upper(),
                },
            )
            trace = _tool_evidence(answer, "get_live_order_tracking")
            passed = bool(trace and answer.numeric_guard_passed and answer.reply_text)
            return SandboxResult(
                passed=passed,
                reply_text=answer.reply_text if passed else "",
                source_tool_call_id=trace if passed else None,
            )
        sku = re.search(r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+){2,}\b", query)
        qty = re.search(r"\b([1-9][0-9]{0,7})\s*(?:pcs|units)\b", query, re.I)
        if sku is None or qty is None:
            return SandboxResult(passed=False)
        # Use the same quote policy/guard path as a real proposal, without leaving
        # a draft in the operational approval queue from a readiness check.
        savepoint = await session.begin_nested()
        try:
            answer = await runtime.run_capability(
                session,
                tid,
                AiCapability.SALES_QUOTING,
                {
                    "message": query,
                    "items": [{"sku": sku.group(), "qty": int(qty[1])}],
                },
            )
        finally:
            await savepoint.rollback()
        trace = _tool_evidence(answer, "calc_b2b_quote_matrix")
        passed = bool(
            trace and answer.proposal_type == ProposalType.QUOTE_DRAFT and answer.proposal_id
        )
        return SandboxResult(
            passed=passed,
            reply_text="A grounded quote draft passed margin policy." if passed else "",
            source_tool_call_id=trace if passed else None,
        )
    except RuntimeError:
        raise proposal_error(
            503, "ERR_AI_PROVIDER_UNAVAILABLE", "AI provider unavailable."
        ) from None
