from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
from erp_core.ai_engine.models import AiCreativeAsset, AiEmployee, QuickReplyTemplate
from erp_core.ai_engine.numeric_guard import ToolGroundedNumericGuard
from erp_core.ai_engine.proposal_executor import visible_proposal_types
from erp_core.ai_engine.router_llm import (
    MODEL_CATALOG,
    LlmGenerationResult,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.ai_engine.workforce_schemas import (
    CAPABILITIES_HREF,
    PROPOSAL_TYPE_LABELS,
    PROPOSALS_HREF,
    AiEmployeeCreate,
    AiEmployeeRead,
    AiEmployeeUpdate,
    AutoExecutedCapability,
    AutonomySummary,
    CreativeAssetCreate,
    CreativeAssetRead,
    CreativeAssetStatus,
    CreativeAssetUpdate,
    InboxAssistAction,
    InboxAssistRequest,
    InboxAssistResponse,
    LockedGuardrail,
    PendingProposalGroup,
    QuickReplyCreate,
    QuickReplyRead,
    WorkforceKpis,
    WorkforceMatrixResponse,
)
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.shared.enums import (
    AiCapability,
    AutonomyLevel,
    Incoterm,
    ProposalStatus,
    ProposalType,
    UsageMetricType,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, page_of, record_not_found
from erp_core.tenancy.audit import record_audit_event

_LOOKBACK = timedelta(days=7)
_COST_QUANTUM = Decimal("0.000001")
_CREDITS_QUANTUM = Decimal("0.0001")
_EXECUTED_STATES = (ProposalStatus.APPROVED.value, ProposalStatus.EDITED_AND_APPROVED.value)
_L0 = AutonomyLevel.L0_COPILOT
_AUTONOMY_ORDER = {_L0: 0, AutonomyLevel.L1_PROPOSAL: 1, AutonomyLevel.L2_AUTO: 2}

# Hard red lines: no autonomy level, configured prompt or model output can execute these.
_L0_GUARDRAILS: tuple[LockedGuardrail, ...] = (
    LockedGuardrail(
        code="send_formal_quote_or_email",
        title="发送正式报价与对外邮件",
        description="正式报价、PI 与对外邮件必须由人工确认后发送。",
        href=CAPABILITIES_HREF,
    ),
    LockedGuardrail(
        code="finance_receipt_settlement",
        title="财务收付与核销",
        description="收款匹配、银行水单核销与结算仅允许人工执行。",
        href=CAPABILITIES_HREF,
    ),
    LockedGuardrail(
        code="refund_and_high_discount",
        title="退款与高额折扣",
        description="退款、补发与超额折扣必须人工审批，AI 只读不写。",
        href=CAPABILITIES_HREF,
    ),
)
_SKU = re.compile(r"\b[A-Z]{2,}-[A-Z0-9]+(?:-[A-Z0-9]+)+\b")
_COMMERCIAL = re.compile(
    r"\b(?:price|pricing|stock|moq|discount|quote)\b|价格|库存|起订量|折扣|报价",
    re.I,
)

_ASSIST_SYSTEM = (
    "You rewrite and summarize B2B cross-border trade messages for a factory sales team. "
    "Never invent prices, stock levels, MOQ tiers, lead times or discounts."
)
_ASSIST_INSTRUCTIONS: dict[InboxAssistAction, str] = {
    InboxAssistAction.POLISH: (
        "Rewrite the draft so it is clear, polite and professional while keeping every fact "
        "unchanged. Return only the rewritten message."
    ),
    InboxAssistAction.SUMMARIZE: (
        "Summarize the conversation in at most four bullet points. Return only the summary."
    ),
    InboxAssistAction.SUGGEST_REPLY: (
        "Suggest one reply the agent can edit. Return only the reply body."
    ),
}


def _error(status: int, code: str, detail: str) -> AppError:
    title = code.removeprefix("ERR_").replace("_", " ")
    return AppError(
        slug=title.lower().replace(" ", "-"),
        title=title.title(),
        status_code=status,
        code=code,
        detail=detail,
    )


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _money(value: Decimal) -> str:
    return str(value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP))


class WorkforceService:
    """AI digital employee, division-of-labour, inbox assist and creative studio service.

    Every metric returned here is computed from real rows (`ai_proposals`,
    `usage_ledger`, `ai_employees`). Nothing is estimated, mocked or hard-coded.
    """

    def __init__(
        self,
        *,
        llm_router: TieredModelRouter | None = None,
        wallet_service: CreditWalletService | None = None,
        tool_registry: ErpToolRegistry | None = None,
        numeric_guard: ToolGroundedNumericGuard | None = None,
    ) -> None:
        self.llm_router = llm_router or TieredModelRouter()
        self.wallet_service = wallet_service or CreditWalletService()
        self.tool_registry = tool_registry or ErpToolRegistry()
        self.numeric_guard = numeric_guard or ToolGroundedNumericGuard()

    # -- digital employees -------------------------------------------------

    @staticmethod
    def _check_autonomy(
        capability_codes: list[AiCapability], level: AutonomyLevel
    ) -> None:
        if not capability_codes:
            return
        ceiling = min_autonomy(
            *(CAPABILITY_SPECS[code].max_allowed_autonomy for code in capability_codes)
        )
        if _AUTONOMY_ORDER[level] > _AUTONOMY_ORDER[ceiling]:
            raise _error(
                422,
                "ERR_AUTONOMY_ABOVE_CAPABILITY_CEILING",
                f"Autonomy {level.value} exceeds the capability ceiling {ceiling.value}.",
            )

    @staticmethod
    def _employee_read(
        employee: AiEmployee, metrics: dict[str, tuple[int, int]]
    ) -> AiEmployeeRead:
        actions = tasks = 0
        for code in employee.capability_codes:
            created, pending = metrics.get(str(code), (0, 0))
            actions += created
            tasks += pending
        return AiEmployeeRead(
            id=employee.id,
            tenant_id=employee.tenant_id,
            code=employee.code,
            display_name=employee.display_name,
            role_title=employee.role_title,
            department=employee.department,
            avatar_emoji=employee.avatar_emoji,
            description=employee.description,
            capability_codes=[AiCapability(code) for code in employee.capability_codes],
            autonomy_level=AutonomyLevel(employee.autonomy_level),
            is_enabled=employee.is_enabled,
            tool_count=employee.tool_count,
            system_persona=employee.system_persona,
            created_at=employee.created_at,
            updated_at=employee.updated_at,
            actions_7d=actions,
            tasks_7d=tasks,
        )

    @staticmethod
    async def _capability_metrics(
        session: AsyncSession, tenant_id: UUID, cutoff: datetime
    ) -> dict[str, tuple[int, int]]:
        rows = (
            await session.execute(
                text("""
                    SELECT capability,
                           COUNT(*) FILTER (WHERE created_at >= :cutoff) AS created_7d,
                           COUNT(*) FILTER (WHERE status = 'pending') AS pending
                    FROM ai_proposals
                    WHERE tenant_id = :tid
                    GROUP BY capability
                """),
                {"tid": tenant_id, "cutoff": cutoff},
            )
        ).all()
        return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}

    async def list_employees(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        department: str | None = None,
        is_enabled: bool | None = None,
    ) -> PaginatedResponse[AiEmployeeRead]:
        filters = [AiEmployee.tenant_id == tenant_id]
        if department:
            filters.append(AiEmployee.department == department)
        if is_enabled is not None:
            filters.append(AiEmployee.is_enabled.is_(is_enabled))
        metrics = await self._capability_metrics(
            session, tenant_id, datetime.now(UTC) - _LOOKBACK
        )
        return await page_of(
            session,
            select(AiEmployee)
            .where(*filters)
            .order_by(AiEmployee.created_at, AiEmployee.id),
            lambda employee: self._employee_read(employee, metrics),
            limit,
            offset,
        )

    async def create_employee(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: AiEmployeeCreate,
        *,
        actor_id: UUID,
    ) -> AiEmployeeRead:
        self._check_autonomy(payload.capability_codes, payload.autonomy_level)
        employee_id = uuid4()
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(AiEmployee).values(
                        id=employee_id,
                        tenant_id=tenant_id,
                        code=payload.code.strip(),
                        display_name=payload.display_name.strip(),
                        role_title=payload.role_title.strip(),
                        department=payload.department.strip(),
                        avatar_emoji=payload.avatar_emoji,
                        description=payload.description.strip(),
                        capability_codes=[code.value for code in payload.capability_codes],
                        autonomy_level=payload.autonomy_level.value,
                        is_enabled=payload.is_enabled,
                        tool_count=payload.tool_count,
                        system_persona=payload.system_persona,
                    )
                )
        except IntegrityError:
            raise conflicting_change(
                f"Employee code '{payload.code.strip()}' already exists in this workspace."
            ) from None
        employee = await session.scalar(
            select(AiEmployee).where(
                AiEmployee.tenant_id == tenant_id, AiEmployee.id == employee_id
            )
        )
        if employee is None:  # pragma: no cover - the insert above already succeeded
            raise record_not_found("AI employee")
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.employee.created",
            "ai_employee",
            employee.id,
            after_state={
                "code": employee.code,
                "autonomy_level": employee.autonomy_level,
                "is_enabled": employee.is_enabled,
            },
        )
        return self._employee_read(employee, {})

    async def update_employee(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        employee_id: UUID,
        payload: AiEmployeeUpdate,
        *,
        actor_id: UUID,
    ) -> AiEmployeeRead:
        employee = await session.scalar(
            select(AiEmployee)
            .where(AiEmployee.tenant_id == tenant_id, AiEmployee.id == employee_id)
            .with_for_update()
        )
        if employee is None:
            raise record_not_found("AI employee")
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        before = {
            "autonomy_level": employee.autonomy_level,
            "is_enabled": employee.is_enabled,
            "capability_codes": list(employee.capability_codes),
        }
        codes = (
            payload.capability_codes
            if payload.capability_codes is not None
            else [AiCapability(code) for code in employee.capability_codes]
        )
        level = payload.autonomy_level or AutonomyLevel(employee.autonomy_level)
        self._check_autonomy(codes, level)
        values: dict[str, Any] = {}
        for field in (
            "display_name",
            "role_title",
            "department",
            "avatar_emoji",
            "description",
            "system_persona",
            "tool_count",
            "is_enabled",
        ):
            if field in changes:
                values[field] = changes[field]
        if "capability_codes" in changes:
            values["capability_codes"] = [code.value for code in codes]
        if "autonomy_level" in changes:
            values["autonomy_level"] = level.value
        if values:
            values["updated_at"] = func.now()
            await session.execute(
                update(AiEmployee)
                .where(AiEmployee.tenant_id == tenant_id, AiEmployee.id == employee_id)
                .values(**values)
            )
            await session.refresh(employee)
            await record_audit_event(
                session,
                tenant_id,
                "human_user",
                str(actor_id),
                "ai.employee.updated",
                "ai_employee",
                employee_id,
                before_state=before,
                after_state={
                    "autonomy_level": employee.autonomy_level,
                    "is_enabled": employee.is_enabled,
                    "capability_codes": list(employee.capability_codes),
                },
            )
        metrics = await self._capability_metrics(
            session, tenant_id, datetime.now(UTC) - _LOOKBACK
        )
        return self._employee_read(employee, metrics)

    # -- division-of-labour matrix ----------------------------------------

    async def matrix(
        self, session: AsyncSession, tenant_id: UUID, role: UserRole
    ) -> WorkforceMatrixResponse:
        now = datetime.now(UTC)
        cutoff = now - _LOOKBACK
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        by_level: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in (
                await session.execute(
                    text(
                        "SELECT autonomy_level, COUNT(*) FROM ai_employees "
                        "WHERE tenant_id = :tid GROUP BY autonomy_level"
                    ),
                    {"tid": tenant_id},
                )
            ).all()
        }
        total_employees = sum(by_level.values())
        active_employees = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM ai_employees "
                    "WHERE tenant_id = :tid AND is_enabled = true"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        actions_7d = int(
            await session.scalar(
                text("""
                    SELECT
                      (SELECT COUNT(*) FROM ai_proposals
                        WHERE tenant_id = :tid AND created_at >= :cutoff)
                    + (SELECT COUNT(*) FROM usage_ledger
                        WHERE tenant_id = :tid AND metric_type = 'ai_credit'
                          AND created_at >= :cutoff)
                """),
                {"tid": tenant_id, "cutoff": cutoff},
            )
            or 0
        )
        today_cost = _decimal(
            await session.scalar(
                text(
                    "SELECT COALESCE(SUM(raw_provider_cost_usd), 0) FROM usage_ledger "
                    "WHERE tenant_id = :tid AND metric_type = 'ai_credit' "
                    "AND created_at >= :start"
                ),
                {"tid": tenant_id, "start": today_start},
            )
        )

        executed_rows = (
            await session.execute(
                text("""
                    SELECT capability, COUNT(*) FROM ai_proposals
                    WHERE tenant_id = :tid AND status = ANY(:states) AND reviewed_at >= :cutoff
                    GROUP BY capability
                """),
                {"tid": tenant_id, "states": list(_EXECUTED_STATES), "cutoff": cutoff},
            )
        ).all()
        executed = {str(row[0]): int(row[1]) for row in executed_rows}
        allowed_types = visible_proposal_types(role)
        pending_rows = (
            await session.execute(
                text("""
                    SELECT proposal_type, COUNT(*) FROM ai_proposals
                    WHERE tenant_id = :tid AND status = 'pending'
                      AND proposal_type = ANY(:types)
                    GROUP BY proposal_type
                """),
                {"tid": tenant_id, "types": allowed_types},
            )
        ).all()
        pending = {str(row[0]): int(row[1]) for row in pending_rows}

        return WorkforceMatrixResponse(
            kpis=WorkforceKpis(
                total_employees=total_employees,
                active_employees=active_employees,
                actions_7d=actions_7d,
                today_llm_cost_usd=_money(today_cost),
                autonomy_summary=AutonomySummary(
                    l2_auto=int(by_level.get(AutonomyLevel.L2_AUTO.value, 0)),
                    l1_proposal=int(by_level.get(AutonomyLevel.L1_PROPOSAL.value, 0)),
                    l0_copilot=int(by_level.get(AutonomyLevel.L0_COPILOT.value, 0)),
                ),
            ),
            l2_auto=[
                AutoExecutedCapability(
                    capability=capability,
                    display_name=spec.display_name,
                    auto_executed_7d=executed[capability.value],
                    href=PROPOSALS_HREF,
                )
                for capability, spec in CAPABILITY_SPECS.items()
                if executed.get(capability.value, 0) > 0
            ],
            l1_pending=[
                PendingProposalGroup(
                    proposal_type=kind,
                    label=PROPOSAL_TYPE_LABELS[kind],
                    count=pending[kind.value],
                    href=PROPOSALS_HREF,
                )
                for kind in ProposalType
                if pending.get(kind.value, 0) > 0
            ],
            l0_locked=list(_L0_GUARDRAILS),
        )

    # -- inbox AI assist ---------------------------------------------------

    async def _generate(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        system: str,
        query: str,
        context: str,
        intent: str,
        capability: AiCapability = AiCapability.CS_SUPPORT,
        max_tokens: int = 600,
    ) -> LlmGenerationResult:
        await self.wallet_service.ensure_wallet(session, tenant_id=tenant_id)
        tier = self.llm_router.select_tier(
            capability=capability, intent=intent, text_length=len(query)
        )
        # Byte length bounds real tokenization; reserve framing tokens per request.
        prompt_upper_bound = len((system + query + context).encode()) + 128
        estimate = max(
            calculate_cost_and_credits(model_id, prompt_upper_bound, 0, max_tokens)[1]
            for model_id, spec in MODEL_CATALOG.items()
            if spec.tier == tier
        )
        if not await self.wallet_service.lock_budget(
            session, tenant_id, max(Decimal(1), estimate)
        ):
            raise _error(402, "ERR_CREDITS_DEPLETED", "Top up to resume AI assistance.")
        generation = await self.llm_router.generate(
            tenant_id=tenant_id,
            capability=capability,
            user_query=query,
            system_prefix=system,
            context_block=context,
            intent=intent,
            cacheable_faq=False,
            max_tokens=max_tokens,
        )
        if not generation.cache_hit:
            allowed, _ = await self.wallet_service.try_reserve_and_record_spend(
                session,
                tenant_id,
                f"ai:{uuid4()}",
                UsageMetricType.AI_CREDIT,
                generation.credits_consumed,
                generation.cost_usd,
                generation.model_id,
            )
            if not allowed:
                raise _error(402, "ERR_CREDITS_DEPLETED", "Top up to resume AI assistance.")
        return generation

    async def _daily_brief(
        self, session: AsyncSession, tenant_id: UUID, language: str
    ) -> InboxAssistResponse:
        now = datetime.now(UTC)
        cutoff = now - _LOOKBACK
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pending_by_type = {
            str(row[0]): int(row[1])
            for row in (
                await session.execute(
                    text(
                        "SELECT proposal_type, COUNT(*) FROM ai_proposals "
                        "WHERE tenant_id = :tid AND status = 'pending' "
                        "GROUP BY proposal_type"
                    ),
                    {"tid": tenant_id},
                )
            ).all()
        }
        open_conversations = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM conversations WHERE tenant_id = :tid "
                    "AND status IN ('open', 'pending_human')"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        low_stock = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM inventory_balances WHERE tenant_id = :tid "
                    "AND on_hand_qty - reserved_qty <= 20"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        shipment_exceptions = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM orders WHERE tenant_id = :tid "
                    "AND fulfillment_status = 'exception'"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        today_cost = _decimal(
            await session.scalar(
                text(
                    "SELECT COALESCE(SUM(raw_provider_cost_usd), 0) FROM usage_ledger "
                    "WHERE tenant_id = :tid AND metric_type = 'ai_credit' "
                    "AND created_at >= :start"
                ),
                {"tid": tenant_id, "start": today_start},
            )
        )
        pending_total = sum(pending_by_type.values())
        executed_7d = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM ai_proposals WHERE tenant_id = :tid "
                    "AND status = ANY(:states) AND reviewed_at >= :cutoff"
                ),
                {"tid": tenant_id, "states": list(_EXECUTED_STATES), "cutoff": cutoff},
            )
            or 0
        )
        data: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "pending_proposals_total": pending_total,
            "pending_proposals_by_type": pending_by_type,
            "executed_proposals_7d": executed_7d,
            "today_llm_cost_usd": _money(today_cost),
            "open_conversations": open_conversations,
            "low_stock_skus": low_stock,
            "shipment_exceptions": shipment_exceptions,
        }
        breakdown = "、".join(
            f"{PROPOSAL_TYPE_LABELS[ProposalType(kind)]} {count}"
            for kind, count in pending_by_type.items()
            if kind in {kind.value for kind in ProposalType}
        )
        if language == "en":
            lines = [
                "Daily AI brief",
                f"- Pending proposals: {pending_total}" + (f" ({breakdown})" if breakdown else ""),
                f"- Executed 7d: {executed_7d}",
                f"- Today LLM cost: {_money(today_cost)} USD",
                f"- Open conversations: {open_conversations}",
                f"- Low stock SKUs: {low_stock}",
                f"- Shipment exceptions: {shipment_exceptions}",
            ]
        else:
            lines = [
                "今日 AI 日报",
                f"- 待审提案：{pending_total} 条" + (f"（{breakdown}）" if breakdown else ""),
                f"- 近 7 天已执行：{executed_7d} 条",
                f"- 今日 LLM 成本：{_money(today_cost)} USD",
                f"- 待处理会话：{open_conversations} 个",
                f"- 库存预警 SKU：{low_stock} 个",
                f"- 履约异常订单：{shipment_exceptions} 单",
            ]
        return InboxAssistResponse(
            action=InboxAssistAction.DAILY_BRIEF,
            text="\n".join(lines),
            model_used="deterministic_sql_brief",
            data=data,
        )

    async def inbox_assist(
        self, session: AsyncSession, tenant_id: UUID, payload: InboxAssistRequest
    ) -> InboxAssistResponse:
        if payload.action is InboxAssistAction.DAILY_BRIEF:
            return await self._daily_brief(session, tenant_id, payload.language)
        source = (payload.text or "").strip()
        if not source:
            raise _error(422, "ERR_ASSIST_TEXT_REQUIRED", "Provide text to assist with.")
        instruction = _ASSIST_INSTRUCTIONS[payload.action]
        tool_calls: list[dict[str, Any]] = []
        context_lines: list[str] = []
        sku = _SKU.search(source)
        if sku is not None:
            live = await self.tool_registry.get_live_sku_pricing_and_stock(
                session, tenant_id, sku.group(), qty=1, incoterm=Incoterm.FOB
            )
            tool_calls.append(
                {"tool_name": live.tool_name, "tool_call_id": live.tool_call_id, "ok": live.ok}
            )
            if live.ok:
                context_lines.append(
                    "Verified ERP facts (quote them exactly, do not reformat): "
                    f"sku={live.data['sku']}, available_qty={live.data['available_qty']}, "
                    f"moq={live.data['applied_moq_tier']}, "
                    f"unit_price_usd={live.data['applied_unit_price_usd']}"
                )
        generation = await self._generate(
            session,
            tenant_id,
            system=_ASSIST_SYSTEM,
            query=f"{instruction}\n\nMessage:\n{source}",
            context="\n".join(context_lines),
            intent="translate" if payload.action is InboxAssistAction.POLISH else "summary",
        )
        reply = generation.text.strip()
        guard = self.numeric_guard.verify(reply, [])
        # The output lands in a human-editable draft; a failed guard only raises the
        # review flag so a fabricated figure can never be presented as verified.
        requires_review = (not guard.passed) and bool(_COMMERCIAL.search(reply))
        return InboxAssistResponse(
            action=payload.action,
            text=reply,
            numeric_guard_passed=guard.passed,
            requires_human_review=requires_review,
            model_used=generation.model_id,
            credits_consumed=str(
                generation.credits_consumed.quantize(_CREDITS_QUANTUM, rounding=ROUND_HALF_UP)
            ),
            tool_calls=tool_calls,
        )

    # -- quick reply templates --------------------------------------------

    async def list_quick_replies(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        language: str | None = None,
        category: str | None = None,
    ) -> PaginatedResponse[QuickReplyRead]:
        filters = [QuickReplyTemplate.tenant_id == tenant_id]
        if language:
            filters.append(QuickReplyTemplate.language == language)
        if category:
            filters.append(QuickReplyTemplate.category == category)
        return await page_of(
            session,
            select(QuickReplyTemplate)
            .where(*filters)
            .order_by(QuickReplyTemplate.created_at, QuickReplyTemplate.id),
            QuickReplyRead.model_validate,
            limit,
            offset,
        )

    async def create_quick_reply(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: QuickReplyCreate,
        *,
        actor_id: UUID,
    ) -> QuickReplyRead:
        template_id = uuid4()
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(QuickReplyTemplate).values(
                        id=template_id,
                        tenant_id=tenant_id,
                        title=payload.title.strip(),
                        category=payload.category.strip(),
                        language=payload.language,
                        body_text=payload.body_text.strip(),
                        shortcut_code=payload.shortcut_code.strip(),
                    )
                )
        except IntegrityError:
            raise conflicting_change(
                f"Shortcut '{payload.shortcut_code.strip()}' is already used."
            ) from None
        template = await session.scalar(
            select(QuickReplyTemplate).where(
                QuickReplyTemplate.tenant_id == tenant_id,
                QuickReplyTemplate.id == template_id,
            )
        )
        if template is None:  # pragma: no cover - insert already succeeded
            raise record_not_found("Quick reply template")
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.quick_reply.created",
            "quick_reply_template",
            template.id,
            after_state={"shortcut_code": template.shortcut_code},
        )
        return QuickReplyRead.model_validate(template)

    async def delete_quick_reply(
        self, session: AsyncSession, tenant_id: UUID, template_id: UUID, *, actor_id: UUID
    ) -> None:
        template = await session.scalar(
            select(QuickReplyTemplate).where(
                QuickReplyTemplate.tenant_id == tenant_id,
                QuickReplyTemplate.id == template_id,
            )
        )
        if template is None:
            raise record_not_found("Quick reply template")
        await session.execute(
            text(
                "DELETE FROM quick_reply_templates WHERE tenant_id = :tid AND id = :id"
            ),
            {"tid": tenant_id, "id": template_id},
        )
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.quick_reply.deleted",
            "quick_reply_template",
            template_id,
            before_state={"shortcut_code": template.shortcut_code},
        )

    # -- creative studio ---------------------------------------------------

    async def list_assets(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int,
        offset: int,
        asset_type: str | None = None,
        status: str | None = None,
    ) -> PaginatedResponse[CreativeAssetRead]:
        filters = [AiCreativeAsset.tenant_id == tenant_id]
        if asset_type:
            filters.append(AiCreativeAsset.asset_type == asset_type)
        if status:
            filters.append(AiCreativeAsset.status == status)
        return await page_of(
            session,
            select(AiCreativeAsset)
            .where(*filters)
            .order_by(AiCreativeAsset.created_at.desc(), AiCreativeAsset.id),
            CreativeAssetRead.model_validate,
            limit,
            offset,
        )

    async def create_asset(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: CreativeAssetCreate,
        *,
        actor_id: UUID,
    ) -> CreativeAssetRead:
        content = (payload.content_text or "").strip()
        tool_calls: list[dict[str, Any]] = []
        guard_passed = True
        if payload.generate:
            if content:
                raise _error(
                    422,
                    "ERR_ASSET_CONTENT_CONFLICT",
                    "Provide either content_text or generate=true, not both.",
                )
            prompt = (payload.prompt or "").strip()
            if not prompt:
                raise _error(
                    422, "ERR_ASSET_PROMPT_REQUIRED", "AI generation requires a prompt."
                )
            context_lines: list[str] = []
            if payload.sku_ref:
                live = await self.tool_registry.get_live_sku_pricing_and_stock(
                    session, tenant_id, payload.sku_ref, qty=1, incoterm=Incoterm.FOB
                )
                tool_calls.append(
                    {
                        "tool_name": live.tool_name,
                        "tool_call_id": live.tool_call_id,
                        "ok": live.ok,
                    }
                )
                if not live.ok:
                    if live.error_code == "ERR_SKU_NOT_FOUND":
                        raise _error(
                            422,
                            "ERR_ASSET_SKU_UNKNOWN",
                            f"SKU '{payload.sku_ref}' is not in this workspace catalog.",
                        )
                    raise _error(
                        422,
                        "ERR_ASSET_PRICING_UNAVAILABLE",
                        f"Live pricing for '{payload.sku_ref}' is unavailable "
                        f"({live.error_code or 'unknown'}).",
                    )
                context_lines.append(
                    "Verified ERP facts (quote exactly, do not invent other numbers): "
                    f"sku={live.data['sku']}, moq={live.data['applied_moq_tier']}, "
                    f"available_qty={live.data['available_qty']}, "
                    f"unit_price_usd={live.data['applied_unit_price_usd']}"
                )
            generation = await self._generate(
                session,
                tenant_id,
                system=(
                    "You are a B2B content studio assistant for a factory. Write marketing "
                    "copy that is factual, compliant and free of unverifiable claims."
                ),
                query=f"Asset type: {payload.asset_type.value}\nLanguage: {payload.language}\n"
                f"Brief: {prompt}",
                context="\n".join(context_lines),
                intent="content_studio",
                capability=AiCapability.CONTENT_STUDIO,
            )
            content = generation.text.strip()
            if not content:
                raise _error(
                    502, "ERR_AI_EMPTY_COMPLETION", "The provider returned no content."
                )
            guard_passed = self.numeric_guard.verify(content, []).passed
        if not content:
            raise _error(422, "ERR_ASSET_CONTENT_REQUIRED", "Provide content_text.")
        asset_id = uuid4()
        await session.execute(
            insert(AiCreativeAsset).values(
                id=asset_id,
                tenant_id=tenant_id,
                asset_type=payload.asset_type.value,
                title=(payload.title or self._default_asset_title(payload)).strip(),
                language=payload.language,
                sku_ref=payload.sku_ref,
                prompt=payload.prompt,
                content_text=content,
                # A generated draft always starts in draft; approval stays human.
                status=CreativeAssetStatus.DRAFT.value,
                created_by=actor_id,
            )
        )
        asset = await session.scalar(
            select(AiCreativeAsset).where(
                AiCreativeAsset.tenant_id == tenant_id, AiCreativeAsset.id == asset_id
            )
        )
        if asset is None:  # pragma: no cover - insert already succeeded
            raise record_not_found("Creative asset")
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(actor_id),
            "ai.studio.asset.created",
            "ai_creative_asset",
            asset_id,
            after_state={"asset_type": asset.asset_type, "sku_ref": asset.sku_ref},
        )
        return CreativeAssetRead.model_validate(asset).model_copy(
            update={"numeric_guard_passed": guard_passed, "tool_calls": tool_calls}
        )

    @staticmethod
    def _default_asset_title(payload: CreativeAssetCreate) -> str:
        return f"{payload.asset_type.value} · {payload.sku_ref or payload.language}"

    async def update_asset(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        asset_id: UUID,
        payload: CreativeAssetUpdate,
        *,
        actor_id: UUID,
    ) -> CreativeAssetRead:
        asset = await session.scalar(
            select(AiCreativeAsset)
            .where(AiCreativeAsset.tenant_id == tenant_id, AiCreativeAsset.id == asset_id)
            .with_for_update()
        )
        if asset is None:
            raise record_not_found("Creative asset")
        values: dict[str, Any] = {}
        for field in ("title", "language", "content_text"):
            value = getattr(payload, field)
            if value is not None:
                values[field] = value.strip() if isinstance(value, str) else value
        if payload.status is not None:
            values["status"] = payload.status.value
        if values:
            values["updated_at"] = func.now()
            await session.execute(
                update(AiCreativeAsset)
                .where(AiCreativeAsset.tenant_id == tenant_id, AiCreativeAsset.id == asset_id)
                .values(**values)
            )
            await session.refresh(asset)
            await record_audit_event(
                session,
                tenant_id,
                "human_user",
                str(actor_id),
                "ai.studio.asset.updated",
                "ai_creative_asset",
                asset_id,
                after_state={"status": asset.status},
            )
        return CreativeAssetRead.model_validate(asset)
