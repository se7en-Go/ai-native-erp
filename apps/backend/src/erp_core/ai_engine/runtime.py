from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
from erp_core.ai_engine.compliance_filter import WHATSAPP_CHANNELS, OutboundComplianceFilter
from erp_core.ai_engine.ingestion import has_dynamic_pricing_signals
from erp_core.ai_engine.models import AiProposal
from erp_core.ai_engine.numeric_guard import ToolGroundedNumericGuard
from erp_core.ai_engine.rag_service import HybridRagService, RagHit, verify_strict_citations
from erp_core.ai_engine.router_llm import (
    MODEL_CATALOG,
    LlmGenerationResult,
    TieredModelRouter,
    calculate_cost_and_credits,
)
from erp_core.ai_engine.tools import ErpToolRegistry, ToolResult
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.shared.enums import (
    AiCapability,
    AutonomyLevel,
    ChannelType,
    Incoterm,
    ProposalType,
    UsageMetricType,
)
from erp_core.tenancy.audit import record_audit_event

_L0 = AutonomyLevel.L0_COPILOT
_L1 = AutonomyLevel.L1_PROPOSAL
_L2 = AutonomyLevel.L2_AUTO
_HARD_ACTIONS = frozenset(
    {
        "refund",
        "issue_refund",
        "high_discount",
        "approve_discount",
        "payment_match",
        "confirm_payment_settlement",
        "payment_settlement",
        "verify_bank_slip",
        "bank_slip",
        "send_formal_quote",
        "send_quote",
        "sign_contract",
        "write_off",
    }
)
_HARD_TEXT = re.compile(
    r"\b(?:refund|write[ -]off|(?:confirm|verify|settle)\s+(?:the\s+)?(?:payment|bank)|"
    r"(?:send|issue)\s+(?:the\s+)?(?:formal\s+)?(?:quote|quotation)|"
    r"(?:high|large)\s+discount)\b|退款|核销|银行水单|发送正式报价|高额折扣",
    re.I,
)
_ORDER = re.compile(r"\bORD-[A-Z0-9-]+\b", re.I)
_SKU = re.compile(r"\b[A-Z]{2,}-[A-Z0-9]+(?:-[A-Z0-9]+)+\b", re.I)
_LIVE = re.compile(
    r"\b(?:price|pricing|cost|discount|stock|inventory|moq|quote|tracking|shipment status)\b|"
    r"售价|价格|库存|起订量|单号|物流状态",
    re.I,
)
_ARCHIVE = re.compile(r"\b(?:old|legacy|archived?|historical)\b.*\b(?:price|pricing)\b", re.I)
_AFTER_SALES = re.compile(r"\b(?:damaged|defective|broken|reship|replacement)\b|破损|补发", re.I)
_REDACTED = "[PRICE_REDACTED_USE_LIVE_ERP_TOOL]"
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ANALYTICS_WINDOW_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b7\s*d\b|\b7\s*days?\b|\b7\s*-\s*day\b|近\s*7|7\s*天|7\s*日", re.I), "7d"),
    (
        re.compile(
            r"\b90\s*d\b|\b90\s*days?\b|\b90\s*-\s*day\b|近\s*90|90\s*天|90\s*日|季度|quarter",
            re.I,
        ),
        "90d",
    ),
)
#: Natural-language platform mentions mapped onto `channel_accounts.channel_type` values.
_ANALYTICS_CHANNEL_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("shopify", ("shopify",)),
    ("shopee", ("shopee", "虾皮")),
    ("temu", ("temu",)),
    ("tiktok_shop", ("tiktok", "tik tok", "tiktok shop", "抖音")),
    ("amazon_sp", ("amazon", "亚马逊")),
    ("ebay", ("ebay",)),
    ("alibaba_email", ("alibaba", "阿里国际站")),
    ("aliexpress", ("aliexpress", "速卖通")),
    ("woocommerce", ("woocommerce", "woo commerce")),
    ("shopline", ("shopline",)),
    ("shoplazza", ("shoplazza",)),
    ("lazada", ("lazada", "来赞达")),
    ("shein", ("shein", "希音")),
    ("ozon", ("ozon",)),
    ("walmart", ("walmart", "沃尔玛")),
    ("mercado_libre", ("mercado libre", "mercado_libre", "美客多")),
)


class CapabilityExecutionResult(BaseModel):
    capability: AiCapability
    autonomy_applied: AutonomyLevel
    auto_executed: bool = False
    l0_locked: bool = False
    degraded_to_l0: bool = False
    should_abstain: bool = False
    compliance_blocked: bool = False
    compliance_violations: list[str] = Field(default_factory=list)
    numeric_guard_passed: bool = True
    proposal_id: UUID | None = None
    proposal_type: ProposalType | None = None
    reply_text: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    estimated_margin_pct: Decimal | None = None
    credits_consumed: Decimal = Decimal("0")
    model_used: str = ""
    reason: str | None = None


def compute_next_follow_up_utc(
    iana_timezone: str | None,
    now_utc: datetime | None = None,
) -> datetime:
    now = now_utc or datetime.now(UTC)
    try:
        within = now.astimezone(ZoneInfo(iana_timezone)) if iana_timezone else None
    except ZoneInfoNotFoundError:
        within = None
    if within is None:
        return now + timedelta(hours=1)
    if 8 <= within.hour < 21:
        return now
    day = within + timedelta(days=1) if within.hour >= 21 else within
    return day.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)


def _evidence_lines(hits: list[RagHit], archive: bool) -> list[str]:
    evidence = []
    for hit in hits:
        for line in re.split(r"(?<=[.!?。])\s+|\n", hit.content):
            claim = line.strip().lstrip("# ").rstrip(".!?。")
            if not claim or claim == _REDACTED and not archive:
                continue
            if claim == _REDACTED or (
                not has_dynamic_pricing_signals(claim) and not _LIVE.search(claim)
            ):
                evidence.append(f"{claim} [{hit.doc_id}#{hit.chunk_id}].")
    return evidence


async def load_ai_policy(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    lock: bool = False,
) -> dict[str, Any] | None:
    query = "SELECT CAST(ai_policy_settings AS text) FROM tenants WHERE id = :tid"
    raw = await session.scalar(text(query + (" FOR UPDATE" if lock else "")), {"tid": tenant_id})
    policy = json.loads(raw) if raw is not None else None
    return policy if isinstance(policy, dict) else None


def _analytics_window(input_payload: dict[str, Any], query: str) -> str:
    """Resolve the analytics window: explicit override first, then natural-language hints."""
    explicit = input_payload.get("window")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    for pattern, window in _ANALYTICS_WINDOW_HINTS:
        if pattern.search(query):
            return window
    return "30d"


def _analytics_channel_types(
    input_payload: dict[str, Any],
    query: str,
) -> list[str] | None:
    """Resolve channel filters: explicit override first, then mentioned platforms."""
    explicit = input_payload.get("channel_types")
    if isinstance(explicit, list):
        requested = [str(value).strip().lower() for value in explicit if str(value).strip()]
        if requested:
            return requested
    lowered = query.lower()
    detected = [
        channel_type
        for channel_type, aliases in _ANALYTICS_CHANNEL_ALIASES
        if any(alias in lowered for alias in aliases)
    ]
    return detected or None


def _analytics_claims(
    tool_call_id: str,
    totals: dict[str, Any],
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build guard claims that each resolve to exactly one trusted analytics row."""
    rows: list[dict[str, Any]] = [
        {
            "source_tool_call_id": tool_call_id,
            "window": totals.get("window"),
            "channel_type": totals.get("channel_type"),
            "order_count": totals.get("order_count"),
            "gmv_base": totals.get("gmv_base"),
            "refunded_amount_base": totals.get("refunded_amount_base"),
            "refund_rate_pct": totals.get("refund_rate_pct"),
            "net_margin_base": totals.get("net_margin_base"),
            "margin_pct": totals.get("margin_pct"),
            "roas": totals.get("roas"),
        },
        *(
            {
                "source_tool_call_id": tool_call_id,
                "channel_type": row.get("channel_type"),
                "channel_label": row.get("channel_label"),
                "gmv_base": row.get("gmv_base"),
                "refund_rate_pct": row.get("refund_rate_pct"),
                "margin_pct": row.get("margin_pct"),
                "roas": row.get("roas"),
            }
            for row in channels[:5]
        ),
    ]
    return [{key: value for key, value in row.items() if value is not None} for row in rows]


def _analytics_reply(
    query: str,
    tool_call_id: str,
    totals: dict[str, Any],
    channels: list[dict[str, Any]],
) -> str:
    """Compose a concise bilingual summary citing only exact tool values."""
    top = channels[:3]
    roas_label = "未投放广告" if _CJK.search(query) else "no ad spend"
    if _CJK.search(query):
        lines = [
            f"渠道销售分析（{totals.get('window')}）：GMV {totals.get('gmv_base')}，"
            f"退款额 {totals.get('refunded_amount_base')}"
            f"（退款率 {totals.get('refund_rate_pct')}%），"
            f"广告花费 {totals.get('ad_spend_base')}，"
            f"净毛利 {totals.get('net_margin_base')}（毛利率 {totals.get('margin_pct')}%），"
            f"ROAS {totals.get('roas') or roas_label}，订单数 {totals.get('order_count')}。",
        ]
        lines.extend(
            f"{row.get('channel_label')}（{row.get('channel_type')}）："
            f"GMV {row.get('gmv_base')}，毛利率 {row.get('margin_pct')}%，"
            f"ROAS {row.get('roas') or roas_label}，订单数 {row.get('order_count')}。"
            for row in top
        )
        lines.append(f"数据来源 source_tool_call_id: {tool_call_id}")
        return "\n".join(lines)
    lines = [
        f"Channel sales analytics for {totals.get('window')}: GMV {totals.get('gmv_base')}, "
        f"refunds {totals.get('refunded_amount_base')} "
        f"({totals.get('refund_rate_pct')}%), ad spend {totals.get('ad_spend_base')}, "
        f"net margin {totals.get('net_margin_base')} ({totals.get('margin_pct')}%), "
        f"ROAS {totals.get('roas') or roas_label}, orders {totals.get('order_count')}.",
    ]
    lines.extend(
        f"{row.get('channel_label')} ({row.get('channel_type')}): "
        f"GMV {row.get('gmv_base')}, margin {row.get('margin_pct')}%, "
        f"ROAS {row.get('roas') or roas_label}, orders {row.get('order_count')}."
        for row in top
    )
    lines.append(f"Source source_tool_call_id: {tool_call_id}")
    return "\n".join(lines)


class PolicyRuntime:
    """Generate candidates, then apply policy; provider output never grants authority to act."""

    def __init__(
        self,
        *,
        rag_service: HybridRagService | None = None,
        tool_registry: ErpToolRegistry | None = None,
        numeric_guard: ToolGroundedNumericGuard | None = None,
        compliance_filter: OutboundComplianceFilter | None = None,
        llm_router: TieredModelRouter | None = None,
        wallet_service: CreditWalletService | None = None,
    ) -> None:
        self.rag_service = rag_service or HybridRagService()
        self.tool_registry = tool_registry or ErpToolRegistry()
        self.numeric_guard = numeric_guard or ToolGroundedNumericGuard()
        self.compliance_filter = compliance_filter or OutboundComplianceFilter()
        self.llm_router = llm_router or TieredModelRouter()
        self.wallet_service = wallet_service or CreditWalletService()

    async def _generate(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        capability: AiCapability,
        query: str,
        context: str,
        intent: str,
        result: CapabilityExecutionResult,
    ) -> LlmGenerationResult | None:
        system = CAPABILITY_SPECS[capability].system_prompt
        tier = self.llm_router.select_tier(
            capability=capability, intent=intent, text_length=len(query)
        )
        max_tokens = 600
        # Byte length bounds normal tokenization; reserve additional framing tokens.
        prompt_upper_bound = len((system + query + context).encode()) + 128
        estimate = max(
            calculate_cost_and_credits(model_id, prompt_upper_bound, 0, max_tokens)[1]
            for model_id, spec in MODEL_CATALOG.items()
            if spec.tier == tier
        )
        allowed = await self.wallet_service.lock_budget(
            session, tenant_id, max(Decimal(1), estimate)
        )
        if not allowed:
            result.degraded_to_l0 = result.l0_locked = True
            result.reason = "credits_depleted"
            return None
        generation = await self.llm_router.generate(
            tenant_id=tenant_id,
            capability=capability,
            user_query=query,
            system_prefix=system,
            context_block=context,
            intent=intent,
            citations=result.citations,
            cacheable_faq=intent == "faq_static",
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
                result.degraded_to_l0 = result.l0_locked = True
                result.reason = "credits_depleted"
                return None
        return generation

    async def _proposal(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID | None,
        result: CapabilityExecutionResult,
        kind: ProposalType,
        payload: dict[str, Any],
        confidence: Decimal = Decimal("1"),
    ) -> None:
        if (
            result.autonomy_applied == _L0
            or kind not in CAPABILITY_SPECS[result.capability].allowed_proposal_types
        ):
            return
        proposal_id = uuid4()
        await session.execute(
            insert(AiProposal).values(
                id=proposal_id,
                tenant_id=tenant_id,
                capability=result.capability.value,
                proposal_type=kind.value,
                conversation_id=conversation_id,
                proposed_payload=payload,
                citations=result.citations,
                confidence_score=confidence.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                ),
                estimated_margin_pct=result.estimated_margin_pct,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await record_audit_event(
            session,
            tenant_id,
            "ai_agent",
            result.capability.value,
            "ai.proposal.created",
            "ai_proposal",
            proposal_id,
            proposal_id=proposal_id,
            after_state={"proposal_type": kind.value, "status": "pending"},
        )
        result.proposal_id = proposal_id
        result.proposal_type = kind
        result.autonomy_applied = _L1

    @staticmethod
    def _trace(result: CapabilityExecutionResult, tool: ToolResult) -> None:
        result.tool_calls.append(
            {"tool_name": tool.tool_name, "tool_call_id": tool.tool_call_id, "ok": tool.ok}
        )

    async def run_capability(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        capability: AiCapability,
        input_payload: dict[str, Any],
        strict_rag: bool = False,
    ) -> CapabilityExecutionResult:
        spec = CAPABILITY_SPECS[capability]
        result = CapabilityExecutionResult(capability=capability, autonomy_applied=_L0)
        policy = await load_ai_policy(session, tenant_id)
        if policy is None:
            result.reason = "tenant_not_found"
            return result
        action = str(input_payload.get("action", "")).strip().lower().replace("-", "_")
        query = str(input_payload.get("message", "")).strip()
        try:
            level = AutonomyLevel(policy.get(capability.value, spec.default_autonomy))
            channel = ChannelType(input_payload.get("channel_type", ChannelType.WEB_LIVECHAT))
            cid = (
                UUID(str(input_payload["conversation_id"]))
                if input_payload.get("conversation_id")
                else None
            )
        except (TypeError, ValueError):
            result.reason = "invalid_policy_or_context"
            return result
        conversation = None
        if cid is not None:
            conversation = await self.compliance_filter.load_conversation(session, tenant_id, cid)
            if conversation is None:
                result.reason = "conversation_not_found"
                return result
            channel = conversation.channel_type
            if conversation.autonomy_override:
                level = min_autonomy(level, AutonomyLevel(conversation.autonomy_override))
        result.autonomy_applied = min_autonomy(level, spec.max_allowed_autonomy)

        async def abstain(reason: str) -> CapabilityExecutionResult:
            result.autonomy_applied = _L0
            result.should_abstain = True
            result.reason = reason
            result.reply_text = ""
            if cid:
                await self.rag_service.apply_abstention_handoff(
                    session,
                    tenant_id=tenant_id,
                    conversation_id=cid,
                )
            return result

        if (
            action in _HARD_ACTIONS
            or (_HARD_TEXT.search(query) and capability != AiCapability.BIZ_ANALYTICS)
            or capability == AiCapability.FINANCE_RECON
        ):
            result.autonomy_applied = _L0
            result.l0_locked = True
            result.reason = "manual_action_required"
            if (
                capability == AiCapability.FINANCE_RECON
                and not action
                and input_payload.get("amount")
            ):
                try:
                    amount = Decimal(str(input_payload["amount"]))
                    if not amount.is_finite() or not Decimal("0") < amount < Decimal("1e12"):
                        return result
                except InvalidOperation:
                    return result
                match = await self.tool_registry.match_open_receivables(
                    session,
                    tenant_id,
                    amount,
                    str(input_payload.get("currency", "USD")),
                    customer_id=conversation.customer_id if conversation else None,
                    reference_hint=input_payload.get("reference_hint"),
                )
                self._trace(result, match)
                candidates = match.data.get("candidates", [])
                result.reply_text = (
                    f"{len(candidates)} potential receivable match(es); verify manually."
                )
            return result
        if result.autonomy_applied == _L0:
            return result.model_copy(update={"reason": "manual_policy", "l0_locked": True})
        await self.wallet_service.ensure_wallet(session, tenant_id=tenant_id)
        if not await self.wallet_service.lock_budget(session, tenant_id, Decimal(1)):
            result.degraded_to_l0 = result.l0_locked = True
            return await abstain("credits_depleted")

        mt = input_payload.get("channel_metadata")
        metadata: dict[str, Any] = dict(mt) if isinstance(mt, dict) else {}
        configured_terms = policy.get("outbound_blocked_terms", [])
        if isinstance(configured_terms, list):
            requested_terms = metadata.get("blocked_terms")
            metadata["blocked_terms"] = configured_terms + (
                requested_terms if isinstance(requested_terms, list) else []
            )
        decision = self.compliance_filter.check_outbound(channel, "", channel_metadata=metadata)
        if decision.allowed and conversation and capability != AiCapability.BIZ_ANALYTICS:
            decision = await self.compliance_filter.check_context(
                session, tenant_id, conversation, metadata
            )
        if not decision.allowed:
            result.compliance_blocked = True
            result.compliance_violations = decision.violations
            return await abstain(decision.blocked_reason or "outbound_policy")

        if capability == AiCapability.SALES_QUOTING:
            items = input_payload.get("items")
            if not items:
                await self._proposal(
                    session,
                    tenant_id,
                    cid,
                    result,
                    ProposalType.CRM_ENTITY_EXTRACT,
                    {"requirements": query, "source_channel": channel.value},
                )
                return result
            try:
                currency = str(input_payload.get("currency", "USD")).upper()
                incoterm = Incoterm(input_payload.get("incoterm", "FOB"))
                tool = await self.tool_registry.calc_b2b_quote_matrix(
                    session,
                    tenant_id,
                    items=items,
                    currency=currency,
                    incoterm=incoterm,
                )
            except (TypeError, ValueError, KeyError, InvalidOperation):
                return await abstain("invalid_rfq")
            self._trace(result, tool)
            if not tool.ok:
                return await abstain(tool.error_code or "quote_unavailable")
            result.estimated_margin_pct = Decimal(tool.data["estimated_margin_pct"])
            if tool.data["below_margin_floor"]:
                result.autonomy_applied = _L0
                result.l0_locked = True
                result.reason = "below_margin_floor"
                return result
            await self._proposal(
                session,
                tenant_id,
                cid,
                result,
                ProposalType.QUOTE_DRAFT,
                {
                    "items": items,
                    "quote_currency": currency,
                    "incoterm": incoterm.value,
                    "quote_matrix": tool.data,
                },
            )
            return result

        if capability == AiCapability.BIZ_ANALYTICS:
            raw_sql = str(input_payload.get("sql", "")).strip()
            if raw_sql:
                tool = await self.tool_registry.query_readonly_analytics_sql(
                    session,
                    tenant_id,
                    raw_sql,
                )
                self._trace(result, tool)
                if not tool.ok:
                    return await abstain(tool.error_code or "analytics_unavailable")
                result.reply_text = "Read-only ERP analytics report generated."
                result.tool_calls[-1]["data"] = tool.data
                # Internal typed report, not a generated outbound numeric claim.
                result.auto_executed = result.autonomy_applied == _L2
                return result

            window = _analytics_window(input_payload, query)
            channel_types = _analytics_channel_types(input_payload, query)
            tool = await self.tool_registry.get_live_channel_sales_analytics(
                session,
                tenant_id,
                window=window,
                channel_types=channel_types,
            )
            self._trace(result, tool)
            if not tool.ok:
                return await abstain(tool.error_code or "analytics_unavailable")
            totals: dict[str, Any] = dict(tool.data.get("totals") or {})
            channels: list[dict[str, Any]] = [
                row for row in tool.data.get("channels") or [] if isinstance(row, dict)
            ]
            # `totals` always exists (a zero-order tenant still yields a zeroed row), so
            # provenance is verified even for an empty analytics window.
            verified = self.numeric_guard.verify(
                "",
                [tool],
                _analytics_claims(tool.tool_call_id, totals, channels),
            )
            result.numeric_guard_passed = verified.passed
            result.reply_text = _analytics_reply(query, tool.tool_call_id, totals, channels)
            result.tool_calls[-1]["data"] = tool.data
            result.auto_executed = result.autonomy_applied == _L2
            return result

        if capability == AiCapability.CONTENT_STUDIO:
            target = str(input_payload.get("target_lang", ""))
            intent = "translate" if action == "translate" else "summarize"
            user_input = f"Translate to {target}:\n{query}" if intent == "translate" else query
            generation = await self._generate(
                session, tenant_id, capability, user_input, "", intent, result
            )
            if generation is None:
                return await abstain(result.reason or "model_unavailable")
            result.model_used = generation.model_id
            result.credits_consumed = generation.credits_consumed
            # Internal preview only; it is neither sent nor promoted to a customer reply.
            result.reply_text = generation.text.strip()
            return result

        order_match = _ORDER.search(query)
        order_no = input_payload.get("order_no") or (order_match.group() if order_match else None)
        kind = ProposalType.REPLY_DRAFT
        score = Decimal("1")
        if order_no or capability == AiCapability.ORDER_LOGISTICS:
            if not order_no:
                return await abstain("missing_order_reference")
            if conversation and not await session.scalar(
                text("""
                    SELECT id FROM orders
                    WHERE tenant_id = :tid AND order_no = :number AND customer_id = :customer
                """),
                {
                    "tid": tenant_id,
                    "number": str(order_no).strip(),
                    "customer": conversation.customer_id,
                },
            ):
                return await abstain("order_not_found")
            tool = await self.tool_registry.get_live_order_tracking(
                session, tenant_id, str(order_no)
            )
            self._trace(result, tool)
            if not tool.ok or not tool.data.get("shipments"):
                return await abstain(tool.error_code or "tracking_unavailable")
            claim = [
                {
                    "source_tool_call_id": tool.tool_call_id,
                    "order_no": tool.data["order_no"],
                    "tracking_number": row["tracking_number"],
                    "standardized_status": row["standardized_status"],
                    "carrier_code": row["carrier_code"],
                }
                for row in tool.data["shipments"]
            ]
            claim = [{k: v for k, v in row.items() if v is not None} for row in claim]
            verified = self.numeric_guard.verify("", [tool], claim)
            reply = "\n".join(
                f"Order {row['order_no']}: "
                + "; ".join(
                    str(row[k])
                    for k in ("tracking_number", "standardized_status", "carrier_code")
                    if k in row
                )
                for row in claim
            )
            result.numeric_guard_passed = verified.passed
            recent = datetime.now(UTC) - timedelta(hours=72)
            exception = any(
                row["standardized_status"] in {"Exception", "Expired", "Undelivered"}
                or row.get("last_event_at")
                and row["standardized_status"] != "Delivered"
                and datetime.fromisoformat(row["last_event_at"]) < recent
                for row in tool.data["shipments"]
            )
            # Proactive contact requires L1; status on a customer's inbound request does not.
            if action == "follow_up" or (not query and exception):
                kind = ProposalType.FOLLOW_UP_SUGGESTION
        else:
            sku = _SKU.search(query)
            archive = bool(_ARCHIVE.search(query))
            if not archive and _LIVE.search(query):
                if not sku:
                    return await abstain("live_tool_required")
                try:
                    qty = int(input_payload.get("qty", 1))
                except (TypeError, ValueError):
                    return await abstain("invalid_quantity")
                tool = await self.tool_registry.get_live_sku_pricing_and_stock(
                    session,
                    tenant_id,
                    sku.group(),
                    qty=qty,
                )
                self._trace(result, tool)
                d = tool.data
                context = (
                    (
                        f"For {d['qty_requested']} units of {d['sku']} ({d['incoterm']}), "
                        f"the live unit price is ${d['applied_unit_price_usd']} and "
                        "we currently "
                        f"have {d['available_qty']} units in stock."
                    )
                    if tool.ok
                    else ""
                )
                generation = await self._generate(
                    session, tenant_id, capability, query, context, "pricing_stock", result
                )
                if generation is None:
                    return await abstain(result.reason or "model_unavailable")
                reply = generation.text.strip()
                result.numeric_guard_passed = self.numeric_guard.verify(
                    reply, [tool]
                ).allow_l2_auto_send
            else:
                rag = await self.rag_service.search(
                    session, tenant_id, query, strict_mode=strict_rag
                )
                if rag.should_abstain:
                    return await abstain(rag.abstention_reason or "insufficient_evidence")
                result.citations = [
                    {
                        "doc_id": str(hit.doc_id),
                        "chunk_id": str(hit.chunk_id),
                        "score": hit.rerank_score,
                    }
                    for hit in rag.hits
                ]
                evidence = _evidence_lines(rag.hits, archive)
                if not evidence:
                    return await abstain("no_static_evidence")
                generation = await self._generate(
                    session, tenant_id, capability, query, "\n".join(evidence), "faq_static", result
                )
                if generation is None:
                    return await abstain(result.reason or "model_unavailable")
                reply = generation.text.strip()
                result.numeric_guard_passed = bool(reply) and all(
                    line.strip() in evidence for line in reply.splitlines()
                )
                if strict_rag and not verify_strict_citations(reply, rag.hits).is_valid:
                    result.numeric_guard_passed = False
                score = Decimal(str(rag.top_score))
            result.model_used = generation.model_id
            result.credits_consumed = generation.credits_consumed
            if _AFTER_SALES.search(query):
                kind = ProposalType.REFUND_OR_RESHIP

        decision = self.compliance_filter.check_outbound(channel, reply, channel_metadata=metadata)
        if not decision.allowed or channel in WHATSAPP_CHANNELS and conversation is None:
            result.compliance_blocked = True
            result.compliance_violations = decision.violations or ["missing_whatsapp_conversation"]
            return await abstain(result.compliance_violations[0])
        if not result.numeric_guard_passed:
            return await abstain("grounding_verification_failed")
        result.reply_text = reply
        if result.autonomy_applied == _L1 or kind != ProposalType.REPLY_DRAFT:
            payload: dict[str, Any] = {"body_text": reply, "channel_type": channel.value}
            if kind == ProposalType.FOLLOW_UP_SUGGESTION:
                payload["scheduled_at"] = compute_next_follow_up_utc(
                    input_payload.get("buyer_timezone"),
                ).isoformat()
            await self._proposal(session, tenant_id, cid, result, kind, payload, score)
        else:
            result.auto_executed = True
        return result
