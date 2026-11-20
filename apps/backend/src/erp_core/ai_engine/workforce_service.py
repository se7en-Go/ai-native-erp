from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
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
    DailyPatrolBriefRead,
    InboxAssistAction,
    InboxAssistRequest,
    InboxAssistResponse,
    LockedGuardrail,
    PatrolAlertItem,
    PatrolDispatchRead,
    PatrolFindingItem,
    PatrolSeverity,
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


PatrolFocus = Literal[
    "quoting",
    "orders",
    "warehouse",
    "procurement",
    "logistics",
    "support",
    "finance",
    "analytics",
    "content",
    "social",
    "growth",
    "quality",
]

# Every digital employee's tool grant is derived from its capability scope, so a custom
# employee can never be given a tool its capability does not expose.
CAPABILITY_TOOLS: dict[AiCapability, tuple[str, ...]] = {
    AiCapability.CS_SUPPORT: ("get_live_order_tracking",),
    AiCapability.SALES_QUOTING: (
        "get_live_sku_pricing_and_stock",
        "calc_b2b_quote_matrix",
    ),
    AiCapability.ORDER_LOGISTICS: ("get_live_order_tracking",),
    AiCapability.CONTENT_STUDIO: ("get_live_sku_pricing_and_stock",),
    AiCapability.BIZ_ANALYTICS: ("query_readonly_analytics_sql",),
    AiCapability.FINANCE_RECON: ("match_open_receivables",),
}


@dataclass(frozen=True, slots=True)
class ExportEmployee:
    """One of the twelve default export AI employees shipped with the workspace.

    `code` values are stable: they are the codes the seed script writes into
    `ai_employees`, so existing workspaces keep their roster after an upgrade.
    `aliases` are the pinyin handles operators use when dispatching ("呼叫小单").
    """

    code: str
    display_name: str
    role_title: str
    department: str
    avatar_emoji: str
    description: str
    capability_codes: tuple[str, ...]
    autonomy_level: AutonomyLevel
    tool_count: int
    system_persona: str
    focus: PatrolFocus
    aliases: tuple[str, ...] = ()


DEFAULT_EXPORT_EMPLOYEES: tuple[ExportEmployee, ...] = (
    ExportEmployee(
        code="b2b-sales",
        display_name="小光",
        role_title="B2B 外贸业务员",
        department="商务中心",
        avatar_emoji="💡",
        description="识别 B2B 询盘需求，整理 RFQ 明细并起草报价草案，价格一律取实时 ERP 数据。",
        capability_codes=("sales_quoting", "cs_support"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=6,
        system_persona="严谨的泉州 LED 工厂外贸业务员，只引用 ERP 实时价格与库存。",
        focus="quoting",
        aliases=("xiaoguang",),
    ),
    ExportEmployee(
        code="b2b-order-desk",
        display_name="小单",
        role_title="B端跟单专员",
        department="履约中心",
        avatar_emoji="📋",
        description="跟进 B 端订单进度、生产交期与样品确认，异常自动升级给人工。",
        capability_codes=("sales_quoting", "order_logistics"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=5,
        system_persona="对订单节点极其敏感的跟单专员，从不承诺未确认的交期。",
        focus="orders",
        aliases=("xiaodan",),
    ),
    ExportEmployee(
        code="logistics-forwarder",
        display_name="小海",
        role_title="物流出船专员",
        department="履约中心",
        avatar_emoji="🚢",
        description="查询 17TRACK 实时节点、整理出船文件，货件停滞 72 小时自动预警。",
        capability_codes=("order_logistics",),
        autonomy_level=AutonomyLevel.L2_AUTO,
        tool_count=4,
        system_persona="只转述物流承运商真实节点的出船专员。",
        focus="logistics",
        aliases=("xiaohai", "xiaoan"),
    ),
    ExportEmployee(
        code="growth-scout",
        display_name="小拓",
        role_title="获客专员",
        department="增长中心",
        avatar_emoji="🧭",
        description="整理合规线索池、清洗重复买家并生成首触达话术，禁止越权抓取。",
        capability_codes=("sales_quoting", "cs_support"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=4,
        system_persona="严格遵守各平台 ToS 与退订合规的获客专员。",
        focus="growth",
        aliases=("xiaotuo",),
    ),
    ExportEmployee(
        code="b2c-cs",
        display_name="小暖",
        role_title="B2C 贴心客服",
        department="客户成功",
        avatar_emoji="🌷",
        description="回答零售买家售前售后问题，引用知识库与订单事实，退款一律转人工。",
        capability_codes=("cs_support",),
        autonomy_level=AutonomyLevel.L2_AUTO,
        tool_count=5,
        system_persona="耐心、简洁的 B2C 客服，绝不自行承诺退款或补发。",
        focus="support",
        aliases=("xiaonuan",),
    ),
    ExportEmployee(
        code="finance-assistant",
        display_name="小账",
        role_title="财务助理",
        department="财务中心",
        avatar_emoji="📒",
        description="按金额匹配应收与银行水单候选，核销动作永远由人工确认。",
        capability_codes=("finance_recon",),
        autonomy_level=AutonomyLevel.L0_COPILOT,
        tool_count=3,
        system_persona="只做匹配建议的财务助理，不执行任何收付核销。",
        focus="finance",
        aliases=("xiaozhang",),
    ),
    ExportEmployee(
        code="supply-planner",
        display_name="小供",
        role_title="供应链专员",
        department="供应链中心",
        avatar_emoji="📦",
        description="跟踪安全库存、采购在途与入库进度，输出补货建议。",
        capability_codes=("biz_analytics", "order_logistics"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=4,
        system_persona="只看只读报表的供应链专员，补货决策由人工批准。",
        focus="warehouse",
        aliases=("xiaogong", "xiaocang"),
    ),
    ExportEmployee(
        code="biz-analyst",
        display_name="小析",
        role_title="经营分析师",
        department="经营分析",
        avatar_emoji="📈",
        description="基于只读 ERP 报表输出毛利、渠道结构与库存周转分析。",
        capability_codes=("biz_analytics",),
        autonomy_level=AutonomyLevel.L2_AUTO,
        tool_count=3,
        system_persona="只用批准过的只读 SQL 报表做经营分析的分析师。",
        focus="analytics",
        aliases=("xiaoxi",),
    ),
    ExportEmployee(
        code="content-studio",
        display_name="小文",
        role_title="文案与社媒专员",
        department="品牌营销",
        avatar_emoji="✍️",
        description="为真实 Catalog SKU 生成多语言商品文案、Listing 与社媒配文草稿。",
        capability_codes=("content_studio",),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=3,
        system_persona="不编造参数与认证的多语言文案专员。",
        focus="content",
        aliases=("xiaowen",),
    ),
    ExportEmployee(
        code="social-ops",
        display_name="小社",
        role_title="社媒运营专员",
        department="品牌营销",
        avatar_emoji="📣",
        description="按 14 个平台合规手册排期内容，草稿永远由人工在官方后台确认发布。",
        capability_codes=("content_studio",),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=3,
        system_persona="只做草稿与合规提示的社媒运营，绝不代发人工账号。",
        focus="social",
        aliases=("xiaoshe",),
    ),
    ExportEmployee(
        code="quality-inspector",
        display_name="小质",
        role_title="质检与售后专员",
        department="品质中心",
        avatar_emoji="🔍",
        description="汇总验货与到货异常，整理索赔与补发证据，赔付动作转人工审批。",
        capability_codes=("order_logistics", "cs_support"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=4,
        system_persona="只陈述验货与到货事实的质检专员，不承诺赔付结果。",
        focus="quality",
        aliases=("xiaozhi",),
    ),
    ExportEmployee(
        code="procurement-buyer",
        display_name="小采",
        role_title="采购专员",
        department="供应链中心",
        avatar_emoji="🧾",
        description="按安全库存与在途数据提出采购补货建议，下单动作由人工审批。",
        capability_codes=("biz_analytics", "order_logistics"),
        autonomy_level=AutonomyLevel.L1_PROPOSAL,
        tool_count=4,
        system_persona="只依据实时库存与在途数据提采购建议的采购专员。",
        focus="procurement",
        aliases=("xiaocai",),
    ),
)

_EXPORT_BY_CODE: dict[str, ExportEmployee] = {item.code: item for item in DEFAULT_EXPORT_EMPLOYEES}
_EXPORT_ALIASES: dict[str, str] = {
    alias: item.code for item in DEFAULT_EXPORT_EMPLOYEES for alias in item.aliases
}


def export_employee(code: str) -> ExportEmployee | None:
    """Resolve a canonical code or a pinyin alias onto the locked twelve-role roster."""
    normalized = code.strip().lower()
    roster = _EXPORT_BY_CODE.get(normalized)
    if roster is not None:
        return roster
    aliased = _EXPORT_ALIASES.get(normalized)
    return _EXPORT_BY_CODE.get(aliased) if aliased is not None else None


def capability_tools(codes: list[AiCapability]) -> list[str]:
    """Deterministic tool grant for an employee's capability scope."""
    granted: list[str] = []
    for code in codes:
        for tool in CAPABILITY_TOOLS[code]:
            if tool not in granted:
                granted.append(tool)
    return granted


# -- patrol constants --------------------------------------------------------

# `receivables_payables.status` is the only persisted payment lifecycle in the schema; a
# receivable is still "pending" until it is settled or voided.
_PENDING_PAYMENT_STATUSES = ("open", "partial", "overdue")
# Mirrors the 17TRACK standardized statuses the stalled-shipment worker already alerts on.
_STALLED_SHIPMENT_STATUSES = ("alert", "undelivered", "expired")
_IN_FLIGHT_SHIPMENT_STATUSES = ("info_received", "in_transit", "out_for_delivery")

# Aggregate the per-warehouse balances into one availability number per SKU and honour a
# per-SKU `safety_stock_qty` attribute, defaulting to 50 units when it is absent.
_LOW_STOCK_CTE = """
WITH availability AS (
    SELECT v.sku AS sku,
           SUM(b.on_hand_qty - b.reserved_qty) AS available_qty,
           CASE WHEN (v.attributes ->> 'safety_stock_qty') ~ '^[0-9]+$'
                THEN (v.attributes ->> 'safety_stock_qty')::int
                ELSE 50 END AS safety_stock_qty
      FROM product_variants v
      JOIN inventory_balances b
        ON b.tenant_id = v.tenant_id AND b.variant_id = v.id
     WHERE v.tenant_id = :tid
     GROUP BY v.id, v.sku, v.attributes
)
"""

_PATROL_HREFS: dict[PatrolFocus, str] = {
    "quoting": "/b2b/quotes",
    "orders": "/orders",
    "warehouse": "/wms",
    "procurement": "/supply-chain/purchase-orders",
    "logistics": "/orders",
    "support": "/inbox",
    "finance": "/finance",
    "analytics": "/analytics",
    "content": "/ai/studio",
    "social": "/social",
    "growth": "/crm/leads",
    "quality": "/orders",
}
# Focuses whose finding must leave a real human follow-up in `task_reminders`.
_PATROL_ACTIONABLE_FOCUS: frozenset[str] = frozenset(
    {"quoting", "orders", "warehouse", "procurement", "logistics", "quality"}
)
_PATROL_TASK_DUE_HOURS: dict[str, int] = {
    "quoting": 24,
    "orders": 24,
    "warehouse": 48,
    "procurement": 48,
    "logistics": 12,
    "quality": 12,
}
_CAPABILITY_FOCUS: dict[AiCapability, PatrolFocus] = {
    AiCapability.SALES_QUOTING: "quoting",
    AiCapability.ORDER_LOGISTICS: "logistics",
    AiCapability.CS_SUPPORT: "support",
    AiCapability.BIZ_ANALYTICS: "analytics",
    AiCapability.CONTENT_STUDIO: "content",
    AiCapability.FINANCE_RECON: "finance",
}
_PATROL_SEVERITY_RANK: dict[PatrolSeverity, int] = {"high": 0, "medium": 1, "info": 2}
_PATROL_SEVERITY_WEIGHT: dict[PatrolSeverity, int] = {"high": 6, "medium": 3, "info": 1}
# Each alert may cost at most this many units, so one huge queue cannot zero the score alone.
_PATROL_SEVERITY_CAP = 4


@dataclass(frozen=True, slots=True)
class _PatrolSignal:
    """One live SQL finding, ready to be rendered as an alert or a dispatch result."""

    code: str
    metric: str
    severity: PatrolSeverity
    count: int
    title_zh: str
    title_en: str
    detail_zh: str
    detail_en: str
    employee_code: str
    samples: tuple[dict[str, Any], ...] = ()

    @property
    def href(self) -> str:
        roster = export_employee(self.employee_code)
        focus: PatrolFocus = roster.focus if roster is not None else "analytics"
        return _PATROL_HREFS[focus]


def _export_display_name(code: str) -> str:
    roster = export_employee(code)
    return roster.display_name if roster is not None else code


def _focus_from_capabilities(capability_codes: list[str]) -> PatrolFocus:
    for raw in capability_codes:
        focus = _CAPABILITY_FOCUS.get(AiCapability(raw))
        if focus is not None:
            return focus
    return "analytics"


def _health_score(alerts: list[PatrolAlertItem]) -> int:
    """Deterministic 0..100 health score: severity weight x capped finding count."""
    penalty = sum(
        _PATROL_SEVERITY_WEIGHT[alert.severity] * min(alert.count, _PATROL_SEVERITY_CAP)
        for alert in alerts
    )
    return max(0, min(100, 100 - penalty))


def _patrol_summary_zh(alerts: list[PatrolAlertItem], health_score: int) -> str:
    if not alerts:
        return f"今日巡检未发现需人工介入的异常，健康分 {health_score}。"
    high = sum(1 for alert in alerts if alert.severity == "high")
    medium = sum(1 for alert in alerts if alert.severity == "medium")
    info = sum(1 for alert in alerts if alert.severity == "info")
    detail = "、".join(f"{alert.title_zh} {alert.count}" for alert in alerts)
    return (
        f"今日巡检 {len(alerts)} 项需关注（高优 {high}、中优 {medium}、提示 {info}），"
        f"健康分 {health_score}：{detail}。"
    )


def _patrol_summary_en(alerts: list[PatrolAlertItem], health_score: int) -> str:
    if not alerts:
        return f"Patrol found nothing needing human action today. Health score {health_score}."
    high = sum(1 for alert in alerts if alert.severity == "high")
    medium = sum(1 for alert in alerts if alert.severity == "medium")
    info = sum(1 for alert in alerts if alert.severity == "info")
    detail = ", ".join(f"{alert.title_en} {alert.count}" for alert in alerts)
    return (
        f"Patrol surfaced {len(alerts)} item(s) needing attention (high {high}, medium "
        f"{medium}, info {info}) with a health score of {health_score}: {detail}."
    )


def _patrol_task_title(focus: PatrolFocus, count: int) -> str:
    return {
        "quoting": f"跟进 {count} 张 72 小时内锁汇到期的报价",
        "orders": f"跟进 {count} 张履约异常订单",
        "warehouse": f"补货：{count} 个 SKU 低于安全库存",
        "procurement": f"采购补货：{count} 个 SKU 低于安全库存",
        "logistics": f"核查 {count} 个停滞/异常货件",
        "quality": f"质检与到货异常复核：{count} 个货件",
    }[focus]


def _dispatch_summary_zh(
    employee_name: str, signal: _PatrolSignal, task_id: UUID | None
) -> str:
    summary = f"{employee_name} 已执行「{signal.title_zh}」巡检：{signal.detail_zh}"
    if signal.count == 0:
        return summary + " 当前无需创建待办。"
    if task_id is not None:
        return summary + " 已生成待办任务并指派人工跟进。"
    return summary + " 仅输出巡检结论，不创建待办。"


def _dispatch_summary_en(
    employee_name: str, signal: _PatrolSignal, task_id: UUID | None
) -> str:
    summary = f"{employee_name} ran the '{signal.title_en}' patrol: {signal.detail_en}"
    if signal.count == 0:
        return summary + " No follow-up task was needed."
    if task_id is not None:
        return summary + " A follow-up task was created for a human owner."
    return summary + " Findings only; no task was created."


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


def _amount(value: Any) -> str:
    """Money travels as a string quantized to cents; a float would silently lose them."""
    return str(_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
            allowed_tools=capability_tools(
                [AiCapability(code) for code in employee.capability_codes]
            ),
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
        granted = capability_tools(payload.capability_codes)
        requested = payload.allowed_tools
        if requested is not None:
            outside = [tool for tool in requested if tool not in granted]
            if outside:
                raise _error(
                    422,
                    "ERR_TOOL_OUTSIDE_CAPABILITY",
                    "Tool grants follow the capability scope; "
                    f"{', '.join(sorted(outside))} is not exposed by the declared capabilities.",
                )
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

    # -- proactive operations patrol ---------------------------------------

    async def daily_patrol_brief(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        role: UserRole,
        user_id: UUID,
    ) -> DailyPatrolBriefRead:
        """Live daily patrol: every number below comes from a tenant-scoped SQL query.

        Financial exposure is masked (count 0, no alert) for `AGENT`, and unread
        conversations are narrowed to the agent's own or unassigned threads, so the
        patrol never becomes a side channel around the inbox and finance RBAC.
        """
        signals = [
            await self._expiring_quotes(session, tenant_id),
            await self._low_stock(session, tenant_id),
            await self._stalled_shipments(session, tenant_id),
            await self._unread_conversations(session, tenant_id, user_id=user_id, role=role),
            await self._pending_proposals(session, tenant_id),
            await self._pending_payments(session, tenant_id, role=role),
        ]
        metrics = {signal.metric: signal.count for signal in signals}
        alerts = [
            PatrolAlertItem(
                code=signal.code,
                severity=signal.severity,
                title_zh=signal.title_zh,
                title_en=signal.title_en,
                detail_zh=signal.detail_zh,
                detail_en=signal.detail_en,
                employee_code=signal.employee_code,
                employee_name=_export_display_name(signal.employee_code),
                action_href=signal.href,
                count=signal.count,
            )
            for signal in sorted(
                (item for item in signals if item.count > 0),
                key=lambda item: (_PATROL_SEVERITY_RANK[item.severity], -item.count, item.code),
            )
        ]
        health_score = _health_score(alerts)
        return DailyPatrolBriefRead(
            generated_at=datetime.now(UTC),
            health_score=health_score,
            summary_zh=_patrol_summary_zh(alerts, health_score),
            summary_en=_patrol_summary_en(alerts, health_score),
            metrics=metrics,
            alerts=alerts,
        )

    async def dispatch_patrol(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        employee_code: str,
        *,
        role: UserRole,
        user_id: UUID,
        actor_id: UUID,
    ) -> PatrolDispatchRead:
        """One-click dispatch of a registered digital employee onto live ERP rows."""
        requested = employee_code.strip()
        roster = export_employee(requested)
        candidates = [requested.lower()]
        if roster is not None and roster.code not in candidates:
            candidates.append(roster.code)
        employees = (
            await session.scalars(
                select(AiEmployee).where(
                    AiEmployee.tenant_id == tenant_id,
                    func.lower(AiEmployee.code).in_(candidates),
                )
            )
        ).all()
        if not employees:
            raise record_not_found(f"AI employee '{requested}'")
        employee = next(
            (item for item in employees if item.code.lower() == candidates[0]),
            employees[0],
        )
        if not employee.is_enabled:
            raise _error(
                409,
                "ERR_AI_EMPLOYEE_DISABLED",
                f"AI employee '{employee.code}' is disabled in this workspace.",
            )
        focus = (
            roster.focus
            if roster is not None
            else _focus_from_capabilities(employee.capability_codes)
        )
        if focus == "finance" and role is UserRole.AGENT:
            raise _error(
                403,
                "ERR_PATROL_FINANCE_RESTRICTED",
                "Financial patrols require Owner, Admin, Manager or Finance authority.",
            )
        signal = await self._focus_signal(
            session, tenant_id, focus, role=role, user_id=user_id
        )
        created_task_id: UUID | None = None
        if focus in _PATROL_ACTIONABLE_FOCUS and signal.count > 0:
            due_hours = _PATROL_TASK_DUE_HOURS[focus]
            created_task_id = await session.scalar(
                text("""
                    INSERT INTO task_reminders (tenant_id, title, due_at, priority, status)
                    VALUES (:tid, :title, now() + make_interval(hours => :hours), :priority,
                        'open')
                    RETURNING id
                """),
                {
                    "tid": tenant_id,
                    "title": _patrol_task_title(focus, signal.count),
                    "hours": due_hours,
                    "priority": "high" if signal.severity == "high" else "normal",
                },
            )
        await record_audit_event(
            session,
            tenant_id,
            "ai_agent",
            employee.code,
            "ai.patrol.dispatched",
            "ai_employee",
            employee.id,
            after_state={
                "focus": focus,
                "count": signal.count,
                "created_task_id": str(created_task_id) if created_task_id else None,
                "dispatched_by": str(actor_id),
            },
        )
        return PatrolDispatchRead(
            employee_code=employee.code,
            employee_name=employee.display_name,
            employee_role_title=employee.role_title,
            generated_at=datetime.now(UTC),
            action_summary_zh=_dispatch_summary_zh(employee.display_name, signal, created_task_id),
            action_summary_en=_dispatch_summary_en(employee.display_name, signal, created_task_id),
            metrics={signal.metric: signal.count},
            findings=[
                PatrolFindingItem(
                    code=signal.code,
                    title_zh=signal.title_zh,
                    title_en=signal.title_en,
                    detail_zh=signal.detail_zh,
                    detail_en=signal.detail_en,
                    count=signal.count,
                    action_href=signal.href,
                    samples=list(signal.samples),
                )
            ],
            created_task_id=created_task_id,
            recommended_href=signal.href,
        )

    async def _focus_signal(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        focus: PatrolFocus,
        *,
        role: UserRole,
        user_id: UUID,
    ) -> _PatrolSignal:
        if focus == "quoting":
            return await self._expiring_quotes(session, tenant_id)
        if focus == "orders":
            return await self._order_exceptions(session, tenant_id)
        if focus in {"warehouse", "procurement"}:
            return await self._low_stock(session, tenant_id, focus=focus)
        if focus == "logistics":
            return await self._stalled_shipments(session, tenant_id)
        if focus == "support":
            return await self._unread_conversations(
                session, tenant_id, user_id=user_id, role=role
            )
        if focus == "finance":
            return await self._pending_payments(session, tenant_id, role=role)
        if focus == "growth":
            return await self._active_leads(session, tenant_id)
        if focus == "content":
            return await self._draft_assets(session, tenant_id)
        if focus == "social":
            return await self._social_queue(session, tenant_id)
        if focus == "quality":
            return await self._quality_exceptions(session, tenant_id)
        return await self._pending_proposals(session, tenant_id)

    async def _expiring_quotes(self, session: AsyncSession, tenant_id: UUID) -> _PatrolSignal:
        params = {"tid": tenant_id}
        total = int(
            await session.scalar(
                text("""
                    SELECT COUNT(*) FROM quotes
                    WHERE tenant_id = :tid
                      AND status IN ('draft', 'sent')
                      AND fx_locked_until <= now() + interval '72 hours'
                """),
                params,
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT quote_no, total_amount, quote_currency, fx_locked_until
                    FROM quotes
                    WHERE tenant_id = :tid
                      AND status IN ('draft', 'sent')
                      AND fx_locked_until <= now() + interval '72 hours'
                    ORDER BY fx_locked_until, quote_no
                    LIMIT 3
                """),
                params,
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "quote_no": str(row[0]),
                "total_amount": _amount(row[1]),
                "currency": str(row[2]).strip(),
                "fx_locked_until": row[3].isoformat(),
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['quote_no']}（{item['currency']} {item['total_amount']}，"
            f"{str(item['fx_locked_until'])[:10]} 锁汇到期）"
            for item in samples
        )
        en_items = ", ".join(
            f"{item['quote_no']} ({item['currency']} {item['total_amount']}, FX locked to "
            f"{str(item['fx_locked_until'])[:10]})"
            for item in samples
        )
        return _PatrolSignal(
            code="expiring_quotes",
            metric="expiring_quotes_count",
            severity="high",
            count=total,
            title_zh="锁汇即将到期的报价",
            title_en="Quotes with expiring FX locks",
            detail_zh=f"{total} 张草稿/已发送报价的锁汇将在 72 小时内到期。"
            + (f" 优先跟进：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} draft/sent quote(s) lose their locked FX rate within 72 hours."
            + (f" Follow up first: {en_items}." if en_items else ""),
            employee_code="b2b-sales",
            samples=samples,
        )

    async def _low_stock(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        focus: PatrolFocus = "warehouse",
    ) -> _PatrolSignal:
        params = {"tid": tenant_id}
        total = int(
            await session.scalar(
                text(_LOW_STOCK_CTE + """
                    SELECT COUNT(*) FROM availability
                    WHERE available_qty < safety_stock_qty
                """),
                params,
            )
            or 0
        )
        rows = (
            await session.execute(
                text(_LOW_STOCK_CTE + """
                    SELECT sku, available_qty, safety_stock_qty FROM availability
                    WHERE available_qty < safety_stock_qty
                    ORDER BY available_qty, sku
                    LIMIT 3
                """),
                params,
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "sku": str(row[0]),
                "available_qty": int(row[1]),
                "safety_stock_qty": int(row[2]),
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['sku']}（可售 {item['available_qty']}，安全库存 "
            f"{item['safety_stock_qty']}）"
            for item in samples
        )
        en_items = ", ".join(
            f"{item['sku']} (available {item['available_qty']}, safety stock "
            f"{item['safety_stock_qty']})"
            for item in samples
        )
        return _PatrolSignal(
            code="low_stock_skus",
            metric="low_stock_skus_count",
            severity="high",
            count=total,
            title_zh="低于安全库存的 SKU",
            title_en="SKUs below safety stock",
            detail_zh=f"{total} 个 SKU 的可售数量低于安全库存阈值。"
            + (f" 缺口最大：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} SKU(s) are below their safety stock threshold."
            + (f" Largest gaps: {en_items}." if en_items else ""),
            employee_code="procurement-buyer" if focus == "procurement" else "supply-planner",
            samples=samples,
        )

    async def _stalled_shipments(
        self, session: AsyncSession, tenant_id: UUID
    ) -> _PatrolSignal:
        params = {"tid": tenant_id}
        total = int(
            await session.scalar(
                text("""
                    SELECT COUNT(*) FROM shipments
                    WHERE tenant_id = :tid
                      AND (
                        standardized_status = ANY(:alert_statuses)
                        OR (
                          standardized_status = ANY(:in_flight_statuses)
                          AND COALESCE(last_event_at, created_at) <= now() - interval '72 hours'
                        )
                      )
                """),
                {
                    **params,
                    "alert_statuses": list(_STALLED_SHIPMENT_STATUSES),
                    "in_flight_statuses": list(_IN_FLIGHT_SHIPMENT_STATUSES),
                },
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT tracking_number, carrier_code, standardized_status, last_event_at
                    FROM shipments
                    WHERE tenant_id = :tid
                      AND (
                        standardized_status = ANY(:alert_statuses)
                        OR (
                          standardized_status = ANY(:in_flight_statuses)
                          AND COALESCE(last_event_at, created_at) <= now() - interval '72 hours'
                        )
                      )
                    ORDER BY COALESCE(last_event_at, created_at), tracking_number
                    LIMIT 3
                """),
                {
                    **params,
                    "alert_statuses": list(_STALLED_SHIPMENT_STATUSES),
                    "in_flight_statuses": list(_IN_FLIGHT_SHIPMENT_STATUSES),
                },
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "tracking_number": str(row[0]),
                "carrier_code": str(row[1]) if row[1] else None,
                "standardized_status": str(row[2]),
                "last_event_at": row[3].isoformat() if row[3] else None,
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['tracking_number']}（{item['standardized_status']}）" for item in samples
        )
        en_items = ", ".join(
            f"{item['tracking_number']} ({item['standardized_status']})" for item in samples
        )
        return _PatrolSignal(
            code="stalled_shipments",
            metric="stalled_shipments_count",
            severity="high",
            count=total,
            title_zh="停滞或异常的货件",
            title_en="Stalled or exceptional shipments",
            detail_zh=f"{total} 个货件处于异常状态或超过 72 小时无新节点。"
            + (f" 待核查：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} shipment(s) are in an exception state or silent for 72+ hours."
            + (f" Check: {en_items}." if en_items else ""),
            employee_code="logistics-forwarder",
            samples=samples,
        )

    async def _pending_payments(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        role: UserRole,
    ) -> _PatrolSignal:
        if role is UserRole.AGENT:
            # Financial exposure stays invisible to agents (RBAC parity with finance screens).
            return _PatrolSignal(
                code="pending_payments",
                metric="pending_payments_count",
                severity="medium",
                count=0,
                title_zh="待确认回款",
                title_en="Payments awaiting confirmation",
                detail_zh="当前角色无权查看财务回款数据。",
                detail_en="Financial receivables are masked for this role.",
                employee_code="finance-assistant",
            )
        params = {"tid": tenant_id, "statuses": list(_PENDING_PAYMENT_STATUSES)}
        total = int(
            await session.scalar(
                text("""
                    SELECT COUNT(*) FROM receivables_payables
                    WHERE tenant_id = :tid AND direction = 'receivable'
                      AND status = ANY(:statuses)
                """),
                params,
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT milestone_label, due_amount, currency, due_date, status
                    FROM receivables_payables
                    WHERE tenant_id = :tid AND direction = 'receivable'
                      AND status = ANY(:statuses)
                    ORDER BY due_date NULLS LAST, milestone_label
                    LIMIT 3
                """),
                params,
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "milestone_label": str(row[0]),
                "due_amount": _amount(row[1]),
                "currency": str(row[2]).strip(),
                "due_date": row[3].isoformat() if row[3] else None,
                "status": str(row[4]),
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['milestone_label']}（{item['currency']} {item['due_amount']}，"
            f"{item['due_date'] or '未定账期'}）"
            for item in samples
        )
        en_items = ", ".join(
            f"{item['milestone_label']} ({item['currency']} {item['due_amount']}, "
            f"due {item['due_date'] or 'unscheduled'})"
            for item in samples
        )
        return _PatrolSignal(
            code="pending_payments",
            metric="pending_payments_count",
            severity="medium",
            count=total,
            title_zh="待确认回款",
            title_en="Payments awaiting confirmation",
            detail_zh=f"{total} 笔应收仍处于未结清状态（open/partial/overdue）。"
            + (f" 最近到期：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} receivable(s) are still open, partially paid or overdue."
            + (f" Due next: {en_items}." if en_items else ""),
            employee_code="finance-assistant",
            samples=samples,
        )

    async def _pending_proposals(
        self, session: AsyncSession, tenant_id: UUID
    ) -> _PatrolSignal:
        total = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM ai_proposals WHERE tenant_id = :tid "
                    "AND status = 'pending'"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT proposal_type, COUNT(*) FROM ai_proposals
                    WHERE tenant_id = :tid AND status = 'pending'
                    GROUP BY proposal_type
                    ORDER BY COUNT(*) DESC, proposal_type
                    LIMIT 3
                """),
                {"tid": tenant_id},
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {"proposal_type": str(row[0]), "count": int(row[1])} for row in rows
        )
        breakdown = "、".join(f"{item['proposal_type']} {item['count']}" for item in samples)
        return _PatrolSignal(
            code="pending_proposals",
            metric="pending_proposals_count",
            severity="info",
            count=total,
            title_zh="待审 AI 提案",
            title_en="AI proposals awaiting review",
            detail_zh=f"{total} 条 AI 提案等待人工审批。"
            + (f" 分类：{breakdown}。" if breakdown else ""),
            detail_en=f"{total} AI proposal(s) await human review."
            + (f" Breakdown: {breakdown}." if breakdown else ""),
            employee_code="biz-analyst",
            samples=samples,
        )

    async def _unread_conversations(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        user_id: UUID,
        role: UserRole,
    ) -> _PatrolSignal:
        params: dict[str, Any] = {"tid": tenant_id}
        agent_scope = ""
        if role is UserRole.AGENT:
            # Agents only see their own or unassigned threads, never a colleague's inbox.
            agent_scope = " AND (assigned_user_id IS NULL OR assigned_user_id = :uid)"
            params["uid"] = user_id
        total = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM conversations WHERE tenant_id = :tid "
                    "AND status IN ('open', 'pending_human') AND unread_count > 0" + agent_scope
                ),
                params,
            )
            or 0
        )
        rows = (
            await session.execute(
                text(
                    "SELECT subject, channel_type, unread_count FROM conversations "
                    "WHERE tenant_id = :tid AND status IN ('open', 'pending_human') "
                    "AND unread_count > 0" + agent_scope +
                    " ORDER BY unread_count DESC, updated_at DESC LIMIT 3"
                ),
                params,
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "subject": str(row[0]) if row[0] else "",
                "channel_type": str(row[1]),
                "unread_count": int(row[2]),
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['subject'] or item['channel_type']}（{item['unread_count']} 条未读）"
            for item in samples
        )
        en_items = ", ".join(
            f"{item['subject'] or item['channel_type']} ({item['unread_count']} unread)"
            for item in samples
        )
        return _PatrolSignal(
            code="unread_conversations",
            metric="unread_conversations_count",
            severity="medium",
            count=total,
            title_zh="有未读消息的会话",
            title_en="Conversations with unread messages",
            detail_zh=f"{total} 个进行中的会话仍有未读消息。"
            + (f" 最急：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} live conversation(s) still have unread messages."
            + (f" Most urgent: {en_items}." if en_items else ""),
            employee_code="b2c-cs",
            samples=samples,
        )

    async def _order_exceptions(self, session: AsyncSession, tenant_id: UUID) -> _PatrolSignal:
        total = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM orders WHERE tenant_id = :tid "
                    "AND fulfillment_status = 'exception'"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT order_no, total_amount, currency, payment_status FROM orders
                    WHERE tenant_id = :tid AND fulfillment_status = 'exception'
                    ORDER BY created_at, order_no
                    LIMIT 3
                """),
                {"tid": tenant_id},
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {
                "order_no": str(row[0]),
                "total_amount": _amount(row[1]),
                "currency": str(row[2]).strip(),
                "payment_status": str(row[3]),
            }
            for row in rows
        )
        zh_items = "、".join(
            f"{item['order_no']}（{item['currency']} {item['total_amount']}）" for item in samples
        )
        en_items = ", ".join(
            f"{item['order_no']} ({item['currency']} {item['total_amount']})" for item in samples
        )
        return _PatrolSignal(
            code="order_exceptions",
            metric="order_exceptions_count",
            severity="high",
            count=total,
            title_zh="履约异常订单",
            title_en="Orders in fulfilment exception",
            detail_zh=f"{total} 张订单履约状态为异常，需要跟单确认交期。"
            + (f" 待跟进：{zh_items}。" if zh_items else ""),
            detail_en=f"{total} order(s) are stuck in fulfilment exception."
            + (f" Follow up: {en_items}." if en_items else ""),
            employee_code="b2b-order-desk",
            samples=samples,
        )

    async def _quality_exceptions(
        self, session: AsyncSession, tenant_id: UUID
    ) -> _PatrolSignal:
        signal = await self._stalled_shipments(session, tenant_id)
        return _PatrolSignal(
            code="quality_exceptions",
            metric="quality_exceptions_count",
            severity="high",
            count=signal.count,
            title_zh="到货与质检异常",
            title_en="Delivery and quality exceptions",
            detail_zh=signal.detail_zh,
            detail_en=signal.detail_en,
            employee_code="quality-inspector",
            samples=signal.samples,
        )

    async def _active_leads(self, session: AsyncSession, tenant_id: UUID) -> _PatrolSignal:
        total = int(
            await session.scalar(
                text("""
                    SELECT COUNT(*) FROM leads
                    WHERE tenant_id = :tid AND status IN ('new', 'contacted', 'qualified')
                """),
                {"tid": tenant_id},
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT status, COUNT(*) FROM leads
                    WHERE tenant_id = :tid AND status IN ('new', 'contacted', 'qualified')
                    GROUP BY status
                    ORDER BY COUNT(*) DESC, status
                """),
                {"tid": tenant_id},
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {"status": str(row[0]), "count": int(row[1])} for row in rows
        )
        breakdown = "、".join(f"{item['status']} {item['count']}" for item in samples)
        return _PatrolSignal(
            code="active_leads",
            metric="active_leads_count",
            severity="info",
            count=total,
            title_zh="活跃线索",
            title_en="Active leads",
            detail_zh=f"{total} 条线索仍处于待跟进阶段。"
            + (f" 分布：{breakdown}。" if breakdown else ""),
            detail_en=f"{total} lead(s) are still awaiting follow-up."
            + (f" Breakdown: {breakdown}." if breakdown else ""),
            employee_code="growth-scout",
            samples=samples,
        )

    async def _draft_assets(self, session: AsyncSession, tenant_id: UUID) -> _PatrolSignal:
        total = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM ai_creative_assets WHERE tenant_id = :tid "
                    "AND status = 'draft'"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT asset_type, COUNT(*) FROM ai_creative_assets
                    WHERE tenant_id = :tid AND status = 'draft'
                    GROUP BY asset_type
                    ORDER BY COUNT(*) DESC, asset_type
                """),
                {"tid": tenant_id},
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {"asset_type": str(row[0]), "count": int(row[1])} for row in rows
        )
        breakdown = "、".join(f"{item['asset_type']} {item['count']}" for item in samples)
        return _PatrolSignal(
            code="draft_assets",
            metric="draft_assets_count",
            severity="info",
            count=total,
            title_zh="待审内容草稿",
            title_en="Content drafts awaiting review",
            detail_zh=f"{total} 份内容草稿仍待人工审核。"
            + (f" 分类：{breakdown}。" if breakdown else ""),
            detail_en=f"{total} content draft(s) await human review."
            + (f" Breakdown: {breakdown}." if breakdown else ""),
            employee_code="content-studio",
            samples=samples,
        )

    async def _social_queue(self, session: AsyncSession, tenant_id: UUID) -> _PatrolSignal:
        total = int(
            await session.scalar(
                text("""
                    SELECT COUNT(*) FROM social_posts
                    WHERE tenant_id = :tid AND status IN ('draft', 'scheduled')
                """),
                {"tid": tenant_id},
            )
            or 0
        )
        rows = (
            await session.execute(
                text("""
                    SELECT platform, COUNT(*) FROM social_posts
                    WHERE tenant_id = :tid AND status IN ('draft', 'scheduled')
                    GROUP BY platform
                    ORDER BY COUNT(*) DESC, platform
                """),
                {"tid": tenant_id},
            )
        ).all()
        samples: tuple[dict[str, Any], ...] = tuple(
            {"platform": str(row[0]), "count": int(row[1])} for row in rows
        )
        breakdown = "、".join(f"{item['platform']} {item['count']}" for item in samples)
        return _PatrolSignal(
            code="social_queue",
            metric="social_queue_count",
            severity="info",
            count=total,
            title_zh="社媒内容排期",
            title_en="Social content queue",
            detail_zh=f"{total} 条社媒内容待发布或待确认。"
            + (f" 平台分布：{breakdown}。" if breakdown else ""),
            detail_en=f"{total} social post(s) are queued or awaiting confirmation."
            + (f" By platform: {breakdown}." if breakdown else ""),
            employee_code="social-ops",
            samples=samples,
        )
