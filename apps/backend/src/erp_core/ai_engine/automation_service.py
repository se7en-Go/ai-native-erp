"""Event-driven automation rules over live ERP rows.

Seven deterministic templates, each backed by real tenant-scoped SQL:

* `low_doc_replenishment` - SKUs under safety stock or under the Days-Of-Cover floor,
  from `inventory_balances` + `product_variants` + 30-day `order_items` velocity.
* `negative_margin_hold` - recent orders whose `net_contribution_margin_base` is below
  the configured floor in `order_profit_snapshots`.
* `stalled_shipment_soothe` - shipments that are neither delivered nor cancelled and
  have been silent longer than the configured hours.
* `cold_b2b_quote_followup` - `sent` quotes older than the configured hours that were
  never converted into an order.
* `daily_patrol_brief` - the real `WorkforceService` patrol summary, queued as a P2 brief.
* `negative_review_ticket` - recent inbound customer messages that read like a 1-star
  review / complaint / defect report, turned into one L1 service-ticket proposal per
  message (`category="service_ticket"`).
* `high_adoption_faq_extract` - approved (or edited-and-approved) high-confidence AI
  reply drafts that were never promoted into the knowledge base, turned into one
  structured FAQ Q&A extraction proposal per source proposal.

Every number is `decimal.Decimal` (`ROUND_HALF_UP`) or an integer count read from the
database. A `dry_run` executes the same SQL and returns the same matched counts while
writing nothing at all: no `ai_proposals`, no `notification_deliveries`, no rule
bookkeeping. Proposals are L1 (`status='pending'`) and de-duplicated per rule + entity,
so a repeated run can never flood the review queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.notification_models import AutomationRule
from erp_core.ai_engine.notification_schemas import (
    AutomationEvaluationReport,
    AutomationRuleEvaluationItem,
    AutomationRuleRead,
    AutomationSeedResult,
)
from erp_core.ai_engine.notification_service import NotificationService
from erp_core.ai_engine.workforce_service import WorkforceService
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.query import record_not_found

_CENT = Decimal("0.01")
_VELOCITY_QUANTUM = Decimal("0.0001")
_DAYS_IN_VELOCITY_WINDOW = Decimal("30")
_DEFAULT_SAFETY_STOCK = 50
_RULE_PAYLOAD_KEY = "rule"
#: Hard cap on rows scanned per review/FAQ pass, so one evaluation stays bounded.
_REVIEW_SCAN_LIMIT = 200

#: Default complaint vocabulary for `negative_review_ticket`. Operators may extend or
#: narrow it per tenant through `automation_rules.config_json.keywords`.
DEFAULT_NEGATIVE_REVIEW_KEYWORDS: tuple[str, ...] = (
    "1 star",
    "one star",
    "broken",
    "defective",
    "refund",
    "差评",
    "破损",
    "投诉",
    "bad quality",
)

#: A hit on any of these escalates the digest notification to P0.
_SEVERE_REVIEW_KEYWORDS: frozenset[str] = frozenset(
    {"1 star", "one star", "差评", "投诉", "defective"}
)

#: Default thresholds, one JSONB config per template (`Decimal` values stay strings).
DEFAULT_RULE_TEMPLATES: dict[str, dict[str, Any]] = {
    "low_doc_replenishment": {
        "name": "低库存 / 低可售天数补货",
        "name_en": "Low stock / low Days-Of-Cover replenishment",
        "priority": "P1",
        "autonomy_level": "L1_proposal",
        "config": {"doc_days": 15, "safety_stock_qty": _DEFAULT_SAFETY_STOCK},
    },
    "negative_margin_hold": {
        "name": "负毛利订单拦截",
        "name_en": "Negative contribution margin hold",
        "priority": "P0",
        "autonomy_level": "L0_copilot",
        "config": {"min_margin_pct": "0.00"},
    },
    "stalled_shipment_soothe": {
        "name": "停滞货件安抚",
        "name_en": "Stalled shipment customer soothe",
        "priority": "P1",
        "autonomy_level": "L1_proposal",
        "config": {"stalled_hours": 72},
    },
    "cold_b2b_quote_followup": {
        "name": "沉睡 B2B 报价跟进",
        "name_en": "Cold B2B quote follow-up",
        "priority": "P1",
        "autonomy_level": "L1_proposal",
        "config": {"cold_hours": 72},
    },
    "daily_patrol_brief": {
        "name": "每日经营巡检简报",
        "name_en": "Daily operations patrol brief",
        "priority": "P2",
        "autonomy_level": "L0_copilot",
        "config": {"brief_hour_utc": 1},
    },
    "negative_review_ticket": {
        "name": "差评 / 投诉工单",
        "name_en": "Negative review service ticket",
        "priority": "P1",
        "autonomy_level": "L1_proposal",
        "config": {"lookback_hours": 72, "keywords": list(DEFAULT_NEGATIVE_REVIEW_KEYWORDS)},
    },
    "high_adoption_faq_extract": {
        "name": "高采纳回复转 FAQ",
        "name_en": "High-adoption reply to FAQ extraction",
        "priority": "P2",
        "autonomy_level": "L1_proposal",
        "config": {"min_confidence": "0.900", "lookback_days": 30},
    },
}

#: `kb_documents.source_uri` namespace used to mark an already-promoted AI reply.
FAQ_PROMOTION_URI_PREFIX = "proposal:"

# One availability row per variant: aggregate balance, per-SKU safety stock, 30-day units sold.
_LOW_DOC_SQL = """
WITH availability AS (
    SELECT v.id AS variant_id,
           v.sku AS sku,
           SUM(b.on_hand_qty - b.reserved_qty)::int AS available_qty,
           CASE WHEN (v.attributes ->> 'safety_stock_qty') ~ '^[0-9]+$'
                THEN (v.attributes ->> 'safety_stock_qty')::int
                ELSE :default_safety_stock END AS safety_stock_qty
      FROM product_variants v
      JOIN inventory_balances b
        ON b.tenant_id = v.tenant_id AND b.variant_id = v.id
     WHERE v.tenant_id = :tid
     GROUP BY v.id, v.sku, v.attributes
),
velocity AS (
    SELECT oi.variant_id AS variant_id,
           SUM(oi.quantity)::int AS units_30d
      FROM order_items oi
      JOIN orders o
        ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
     WHERE oi.tenant_id = :tid
       AND o.created_at >= now() - interval '30 days'
     GROUP BY oi.variant_id
)
SELECT a.variant_id,
       a.sku,
       a.available_qty,
       a.safety_stock_qty,
       COALESCE(v.units_30d, 0) AS units_30d
  FROM availability a
  LEFT JOIN velocity v ON v.variant_id = a.variant_id
 ORDER BY a.available_qty, a.sku
"""

_NEGATIVE_MARGIN_SQL = """
SELECT ops.order_id,
       o.order_no,
       ops.net_contribution_margin_base,
       o.currency,
       o.created_at
  FROM order_profit_snapshots ops
  JOIN orders o
    ON o.id = ops.order_id AND o.tenant_id = ops.tenant_id
 WHERE ops.tenant_id = :tid
   AND ops.net_contribution_margin_base < :margin_floor
   AND o.created_at >= now() - interval '7 days'
 ORDER BY ops.net_contribution_margin_base, o.order_no
"""

_STALLED_SHIPMENT_SQL = """
SELECT s.id AS shipment_id,
       s.order_id,
       s.tracking_number,
       s.carrier_code,
       s.standardized_status,
       COALESCE(s.last_event_at, s.created_at) AS stalled_since,
       o.order_no
  FROM shipments s
  JOIN orders o
    ON o.id = s.order_id AND o.tenant_id = s.tenant_id
 WHERE s.tenant_id = :tid
   AND s.standardized_status NOT IN ('delivered', 'cancelled')
   AND COALESCE(s.last_event_at, s.created_at)
       < now() - make_interval(hours => :stalled_hours)
 ORDER BY stalled_since, s.tracking_number
"""

_COLD_QUOTE_SQL = """
SELECT q.id AS quote_id,
       q.quote_no,
       q.quote_currency,
       q.total_amount,
       q.created_at,
       q.opportunity_id,
       COALESCE(bc.legal_name, c.display_name) AS customer_name
  FROM quotes q
  JOIN opportunities opp
    ON opp.id = q.opportunity_id AND opp.tenant_id = q.tenant_id
  JOIN customers c
    ON c.id = opp.customer_id AND c.tenant_id = opp.tenant_id
  LEFT JOIN b2b_companies bc
    ON bc.id = c.company_id AND bc.tenant_id = c.tenant_id
 WHERE q.tenant_id = :tid
   AND q.status = 'sent'
   AND q.converted_order_id IS NULL
   AND q.created_at < now() - make_interval(hours => :cold_hours)
 ORDER BY q.created_at, q.quote_no
"""

#: Recent inbound customer messages, newest first. Keyword matching happens in Python so
#: the operator-editable vocabulary never becomes an interpolated SQL regex.
_NEGATIVE_REVIEW_MESSAGES_SQL = """
SELECT m.id,
       m.conversation_id,
       m.customer_id,
       m.channel_type,
       m.body_text,
       m.source_timestamp,
       c.display_name,
       c.primary_email
  FROM messages m
  LEFT JOIN customers c
    ON c.id = m.customer_id AND c.tenant_id = m.tenant_id
 WHERE m.tenant_id = :tid
   AND m.direction = 'inbound'
   AND m.sender_type = 'customer'
   AND m.body_text IS NOT NULL
   AND m.source_timestamp >= now() - make_interval(hours => :lookback_hours)
 ORDER BY m.source_timestamp DESC, m.id DESC
 LIMIT :row_limit
"""

#: Approved high-confidence reply drafts that were never promoted into `kb_documents`.
#: The promotion marker lives in `kb_documents.source_uri` (`proposal:<id>`), a logical
#: link rather than a hard FK to a table owned by revision `0004`.
_HIGH_ADOPTION_REPLIES_SQL = """
SELECT p.id,
       p.conversation_id,
       p.status,
       p.confidence_score,
       p.proposed_payload,
       p.edited_payload,
       p.reviewed_at,
       (
           SELECT m.body_text
             FROM messages m
            WHERE m.tenant_id = p.tenant_id
              AND m.conversation_id = p.conversation_id
              AND m.direction = 'inbound'
              AND m.body_text IS NOT NULL
            ORDER BY m.source_timestamp DESC, m.id DESC
            LIMIT 1
       ) AS question
  FROM ai_proposals p
 WHERE p.tenant_id = :tid
   AND p.proposal_type = 'reply_draft'
   AND p.status IN ('approved', 'edited_and_approved')
   AND p.confidence_score >= :min_confidence
   AND COALESCE(p.reviewed_at, p.created_at) >= now() - make_interval(days => :lookback_days)
   AND NOT EXISTS (
       SELECT 1
         FROM kb_documents d
        WHERE d.tenant_id = p.tenant_id
          AND d.source_uri = :promotion_prefix || p.id::text
   )
 ORDER BY p.confidence_score DESC, p.id
 LIMIT :row_limit
"""

_PENDING_PROPOSAL_TARGETS_SQL = """
SELECT target_entity_id
  FROM ai_proposals
 WHERE tenant_id = :tid
   AND status = 'pending'
   AND target_entity_id IS NOT NULL
   AND proposed_payload ->> :rule_key = :rule
"""

_INSERT_PROPOSAL_SQL = """
INSERT INTO ai_proposals (
    tenant_id, capability, proposal_type, target_entity_type, target_entity_id,
    proposed_payload, citations, confidence_score, status
)
SELECT :tid, :capability, :proposal_type, :target_entity_type, :target_entity_id,
       CAST(:payload AS jsonb), CAST(:citations AS jsonb), :confidence_score, 'pending'
 WHERE NOT EXISTS (
    SELECT 1
      FROM ai_proposals p
     WHERE p.tenant_id = :tid
       AND p.status = 'pending'
       AND p.target_entity_id = :target_entity_id
       AND p.proposed_payload ->> :rule_key = :rule
 )
RETURNING id
"""


def _error(code: str, detail: str, status_code: int = 422) -> AppError:
    return AppError(
        slug="automation-rule-rejected",
        title="Automation Rule Rejected",
        status_code=status_code,
        detail=detail,
        code=code,
    )


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise _error(
            "ERR_AUTOMATION_CONFIG_INVALID", f"config_json.{key} must be an integer."
        ) from exc
    if value < 0:
        raise _error("ERR_AUTOMATION_CONFIG_INVALID", f"config_json.{key} must be >= 0.")
    return value


def _config_decimal(config: dict[str, Any], key: str, default: Decimal) -> Decimal:
    raw = config.get(key, default)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "ERR_AUTOMATION_CONFIG_INVALID", f"config_json.{key} must be a decimal number."
        ) from exc
    if not value.is_finite():
        raise _error("ERR_AUTOMATION_CONFIG_INVALID", f"config_json.{key} must be finite.")
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _match_review_keywords(body_text: str, keywords: tuple[str, ...]) -> list[str]:
    """Return the configured keywords actually present in an inbound message body."""
    lowered = body_text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def _review_excerpt(body_text: str, limit: int = 280) -> str:
    """One-line excerpt safe for a proposal payload / notification body."""
    collapsed = " ".join(body_text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """Immutable view over a persisted rule, or over a default template in a dry run."""

    template_code: str
    name: str
    is_enabled: bool
    autonomy_level: str
    priority: str
    config_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    capability: str
    proposal_type: str
    target_entity_type: str
    target_entity_id: UUID
    proposed_payload: dict[str, Any]
    citations: tuple[dict[str, Any], ...]
    confidence_score: Decimal


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    event_type: str
    title: str
    body: str
    priority: str
    deep_link: str | None


@dataclass
class RuleOutcome:
    spec: RuleSpec
    matched_count: int
    items_preview: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[ProposalDraft] = field(default_factory=list)
    notifications: list[NotificationDraft] = field(default_factory=list)
    proposals_created: int = 0
    notifications_queued: int = 0
    note: str | None = None

    def to_item(self) -> AutomationRuleEvaluationItem:
        return AutomationRuleEvaluationItem(
            template_code=self.spec.template_code,
            name=self.spec.name,
            is_enabled=self.spec.is_enabled,
            priority=self.spec.priority,
            autonomy_level=self.spec.autonomy_level,
            matched_count=self.matched_count,
            proposals_created=self.proposals_created,
            notifications_queued=self.notifications_queued,
            items_preview=self.items_preview,
            note=self.note,
        )


def _default_specs() -> list[RuleSpec]:
    return [
        RuleSpec(
            template_code=code,
            name=str(spec["name"]),
            is_enabled=True,
            autonomy_level=str(spec["autonomy_level"]),
            priority=str(spec["priority"]),
            config_json=dict(spec["config"]),
        )
        for code, spec in DEFAULT_RULE_TEMPLATES.items()
    ]


def _spec_of(rule: AutomationRule) -> RuleSpec:
    config = rule.config_json if isinstance(rule.config_json, dict) else {}
    return RuleSpec(
        template_code=rule.template_code,
        name=rule.name,
        is_enabled=rule.is_enabled,
        autonomy_level=rule.autonomy_level,
        priority=rule.priority,
        config_json={str(key): value for key, value in config.items()},
    )


class AutomationService:
    """Seeds and evaluates the five event-driven automation templates."""

    def __init__(self, *, workforce_service: WorkforceService | None = None) -> None:
        self._workforce = workforce_service

    def workforce(self) -> WorkforceService:
        if self._workforce is None:
            self._workforce = WorkforceService()
        return self._workforce

    # -- seeding ------------------------------------------------------------

    async def seed_default_rules(
        self, session: AsyncSession, tenant_id: UUID
    ) -> AutomationSeedResult:
        """Idempotently upsert the five default templates (concurrency-safe ON CONFLICT)."""
        existing_before = {
            rule.template_code
            for rule in await session.scalars(
                select(AutomationRule).where(AutomationRule.tenant_id == tenant_id)
            )
        }
        for code, spec in DEFAULT_RULE_TEMPLATES.items():
            if code in existing_before:
                continue
            await session.execute(
                pg_insert(AutomationRule)
                .values(
                    tenant_id=tenant_id,
                    template_code=code,
                    name=str(spec["name"]),
                    is_enabled=True,
                    autonomy_level=str(spec["autonomy_level"]),
                    priority=str(spec["priority"]),
                    config_json=dict(spec["config"]),
                )
                .on_conflict_do_nothing(constraint="uq_automation_rules_tenant_tpl")
            )
        await session.flush()
        rules = (
            await session.scalars(
                select(AutomationRule)
                .where(AutomationRule.tenant_id == tenant_id)
                .order_by(AutomationRule.template_code)
            )
        ).all()
        after = {rule.template_code for rule in rules}
        return AutomationSeedResult(
            created=len(after - existing_before),
            existing=len(existing_before & after),
            rules=[AutomationRuleRead.model_validate(rule) for rule in rules],
        )

    async def list_rules(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[AutomationRuleRead]:
        rules = (
            await session.scalars(
                select(AutomationRule)
                .where(AutomationRule.tenant_id == tenant_id)
                .order_by(AutomationRule.template_code)
            )
        ).all()
        return [AutomationRuleRead.model_validate(rule) for rule in rules]

    async def _load_rule(
        self, session: AsyncSession, tenant_id: UUID, rule_id: UUID
    ) -> AutomationRule:
        rule = await session.scalar(
            select(AutomationRule).where(
                AutomationRule.tenant_id == tenant_id, AutomationRule.id == rule_id
            )
        )
        if rule is None:
            raise record_not_found("Automation rule")
        return rule

    async def update_rule(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        rule_id: UUID,
        *,
        name: str | None = None,
        is_enabled: bool | None = None,
        autonomy_level: str | None = None,
        priority: str | None = None,
        config_json: dict[str, Any] | None = None,
    ) -> AutomationRuleRead:
        rule = await self._load_rule(session, tenant_id, rule_id)
        if name is not None:
            rule.name = name.strip()
        if is_enabled is not None:
            rule.is_enabled = is_enabled
        if autonomy_level is not None:
            rule.autonomy_level = autonomy_level
        if priority is not None:
            rule.priority = priority
        if config_json is not None:
            # Validate the shared threshold vocabulary before it reaches JSONB.
            merged = {**(rule.config_json or {}), **config_json}
            _config_int(merged, "doc_days", 15)
            _config_int(merged, "stalled_hours", 72)
            _config_int(merged, "cold_hours", 72)
            _config_int(merged, "brief_hour_utc", 1)
            _config_int(merged, "lookback_hours", 72)
            _config_int(merged, "lookback_days", 30)
            _config_decimal(merged, "min_margin_pct", Decimal("0.00"))
            _config_decimal(merged, "min_confidence", Decimal("0.900"))
            rule.config_json = merged
        rule.updated_at = datetime.now(UTC)
        await session.flush()
        return AutomationRuleRead.model_validate(rule)

    # -- evaluation ---------------------------------------------------------

    async def evaluate_rules(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        dry_run: bool = False,
        notification_service: NotificationService | None = None,
    ) -> AutomationEvaluationReport:
        """Evaluate every enabled rule. `dry_run=True` reads only and writes nothing."""
        persisted = (
            await session.scalars(
                select(AutomationRule)
                .where(AutomationRule.tenant_id == tenant_id)
                .order_by(AutomationRule.template_code)
            )
        ).all()
        seeded_defaults = False
        if persisted:
            specs = [_spec_of(rule) for rule in persisted]
            rule_ids = {rule.template_code: rule.id for rule in persisted}
        elif dry_run:
            specs = _default_specs()
            rule_ids = {}
        else:
            await self.seed_default_rules(session, tenant_id)
            persisted = (
                await session.scalars(
                    select(AutomationRule)
                    .where(AutomationRule.tenant_id == tenant_id)
                    .order_by(AutomationRule.template_code)
                )
            ).all()
            specs = [_spec_of(rule) for rule in persisted]
            rule_ids = {rule.template_code: rule.id for rule in persisted}
            seeded_defaults = True

        service = notification_service or NotificationService()
        outcomes: list[RuleOutcome] = []
        for spec in specs:
            outcome = await self._evaluate_one(session, tenant_id, spec)
            if not dry_run:
                await self._apply(session, tenant_id, outcome, service)
                rule_id = rule_ids.get(spec.template_code)
                if rule_id is not None:
                    await session.execute(
                        update(AutomationRule)
                        .where(
                            AutomationRule.tenant_id == tenant_id,
                            AutomationRule.id == rule_id,
                        )
                        .values(
                            last_run_at=func.now(),
                            last_triggered_count=outcome.matched_count,
                            updated_at=func.now(),
                        )
                    )
            else:
                outcome.note = (
                    f"dry_run: 计划创建 {len(outcome.proposals)} 条提案 / "
                    f"排队 {len(outcome.notifications)} 条通知，未写入数据库。"
                    if outcome.proposals or outcome.notifications
                    else "dry_run: 未命中任何条目，无需写入。"
                )
            outcomes.append(outcome)
        await session.flush()
        return AutomationEvaluationReport(
            generated_at=datetime.now(UTC),
            dry_run=dry_run,
            seeded_defaults=seeded_defaults,
            total_matched=sum(item.matched_count for item in outcomes),
            total_proposals_created=sum(item.proposals_created for item in outcomes),
            total_notifications_queued=sum(item.notifications_queued for item in outcomes),
            rules=[outcome.to_item() for outcome in outcomes],
        )

    async def _evaluate_one(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        if not spec.is_enabled:
            return RuleOutcome(
                spec=spec, matched_count=0, note="规则已停用，本次跳过 / rule disabled"
            )
        handler = {
            "low_doc_replenishment": self._evaluate_low_doc,
            "negative_margin_hold": self._evaluate_negative_margin,
            "stalled_shipment_soothe": self._evaluate_stalled_shipments,
            "cold_b2b_quote_followup": self._evaluate_cold_quotes,
            "daily_patrol_brief": self._evaluate_patrol_brief,
            "negative_review_ticket": self._evaluate_negative_reviews,
            "high_adoption_faq_extract": self._evaluate_high_adoption_faqs,
        }.get(spec.template_code)
        if handler is None:  # pragma: no cover - the DB CHECK constraint pins the vocabulary
            raise _error(
                "ERR_AUTOMATION_TEMPLATE_UNKNOWN",
                f"Unknown automation template '{spec.template_code}'.",
            )
        return await handler(session, tenant_id, spec)

    async def _pending_targets(
        self, session: AsyncSession, tenant_id: UUID, rule_code: str
    ) -> set[UUID]:
        rows = (
            await session.execute(
                text(_PENDING_PROPOSAL_TARGETS_SQL),
                {"tid": tenant_id, "rule_key": _RULE_PAYLOAD_KEY, "rule": rule_code},
            )
        ).all()
        return {row[0] for row in rows if row[0] is not None}

    async def _evaluate_low_doc(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        doc_days = Decimal(_config_int(spec.config_json, "doc_days", 15))
        default_safety = _config_int(spec.config_json, "safety_stock_qty", _DEFAULT_SAFETY_STOCK)
        rows = (
            await session.execute(
                text(_LOW_DOC_SQL),
                {"tid": tenant_id, "default_safety_stock": default_safety},
            )
        ).all()
        pending = await self._pending_targets(session, tenant_id, spec.template_code)
        outcome = RuleOutcome(spec=spec, matched_count=0)
        for row in rows:
            variant_id = row[0]
            sku = str(row[1])
            available_qty = int(row[2])
            safety_stock_qty = int(row[3])
            units_30d = int(row[4])
            daily_velocity = (
                Decimal(units_30d) / _DAYS_IN_VELOCITY_WINDOW
            ).quantize(_VELOCITY_QUANTUM, rounding=ROUND_HALF_UP)
            days_of_cover = (
                None
                if daily_velocity <= Decimal("0")
                else (Decimal(available_qty) / daily_velocity).quantize(
                    _CENT, rounding=ROUND_HALF_UP
                )
            )
            below_safety = available_qty < safety_stock_qty
            below_cover = days_of_cover is not None and days_of_cover < doc_days
            if not (below_safety or below_cover):
                continue
            outcome.matched_count += 1
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "variant_id": str(variant_id),
                        "sku": sku,
                        "available_qty": available_qty,
                        "safety_stock_qty": safety_stock_qty,
                        "units_sold_30d": units_30d,
                        "daily_velocity": _decimal_text(daily_velocity),
                        "days_of_cover": None
                        if days_of_cover is None
                        else _decimal_text(days_of_cover),
                        "reason": "below_safety_stock" if below_safety else "below_doc_floor",
                    }
                )
            if variant_id in pending:
                continue
            reason = (
                "低于安全库存 / below safety stock"
                if below_safety
                else f"可售天数低于 {doc_days} 天 / Days-Of-Cover below {doc_days} days"
            )
            english_reason = (
                "below safety stock" if below_safety else "below the Days-Of-Cover floor"
            )
            coverage = "无 30 天销量 / no 30-day sales" if days_of_cover is None else (
                f"{_decimal_text(days_of_cover)} 天可售 / days of cover"
            )
            outcome.proposals.append(
                ProposalDraft(
                    capability="order_logistics",
                    proposal_type="follow_up_suggestion",
                    target_entity_type="product_variant",
                    target_entity_id=variant_id,
                    proposed_payload={
                        "rule": spec.template_code,
                        "sku": sku,
                        "available_qty": available_qty,
                        "safety_stock_qty": safety_stock_qty,
                        "units_sold_30d": units_30d,
                        "daily_velocity": _decimal_text(daily_velocity),
                        "days_of_cover": None
                        if days_of_cover is None
                        else _decimal_text(days_of_cover),
                        "thresholds": {
                            "doc_days": str(doc_days),
                            "min_margin_pct": None,
                        },
                        "recommended_action": "create_replenishment_purchase_order",
                        "summary_zh": f"{sku} {reason}（现可售 {available_qty}，安全库存 "
                        f"{safety_stock_qty}，{coverage}）。",
                        "summary_en": f"{sku} is {english_reason}: {available_qty} available "
                        f"vs {safety_stock_qty} safety stock, {coverage}.",
                        "grounding": {
                            "source": "sql",
                            "tables": [
                                "inventory_balances",
                                "product_variants",
                                "order_items",
                                "orders",
                            ],
                            "velocity_window_days": 30,
                        },
                    },
                    citations=(
                        {
                            "kind": "sql_row",
                            "table": "inventory_balances",
                            "variant_id": str(variant_id),
                        },
                    ),
                    confidence_score=Decimal("0.900"),
                )
            )
        if outcome.matched_count:
            sample = "、".join(
                f"{item['sku']}({item['available_qty']})" for item in outcome.items_preview
            )
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.low_doc_replenishment",
                    title=f"补货提醒：{outcome.matched_count} 个 SKU 需要补货 / "
                    f"{outcome.matched_count} SKU(s) need replenishment",
                    body=f"低于安全库存或可售天数 {doc_days} 天：{sample}。"
                    f"\nSKUs below safety stock or the {doc_days}-day cover floor: {sample}.",
                    priority=spec.priority,
                    deep_link="/wms",
                )
            )
        return outcome

    async def _evaluate_negative_margin(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        margin_floor = _config_decimal(spec.config_json, "min_margin_pct", Decimal("0.00"))
        rows = (
            await session.execute(
                text(_NEGATIVE_MARGIN_SQL),
                {"tid": tenant_id, "margin_floor": margin_floor},
            )
        ).all()
        outcome = RuleOutcome(spec=spec, matched_count=0)
        for row in rows:
            order_id = row[0]
            order_no = str(row[1])
            net_margin = Decimal(row[2])
            currency = str(row[3]).strip()
            created_at = row[4]
            outcome.matched_count += 1
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "order_id": str(order_id),
                        "order_no": order_no,
                        "net_contribution_margin_base": _decimal_text(net_margin),
                        "margin_floor": _decimal_text(margin_floor),
                        "currency": currency,
                        "created_at": created_at.isoformat(),
                    }
                )
        if outcome.matched_count:
            sample = "、".join(
                f"{item['order_no']}({item['net_contribution_margin_base']})"
                for item in outcome.items_preview
            )
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.negative_margin_hold",
                    title=f"P0 负毛利拦截：{outcome.matched_count} 张订单毛利低于 "
                    f"{_decimal_text(margin_floor)} / Negative margin hold",
                    body=f"近 7 天毛利低于 {_decimal_text(margin_floor)} 的订单：{sample}。"
                    f"\nOrders from the last 7 days below the margin floor: {sample}.",
                    priority=spec.priority,
                    deep_link="/finance",
                )
            )
        return outcome

    async def _evaluate_stalled_shipments(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        stalled_hours = _config_int(spec.config_json, "stalled_hours", 72)
        rows = (
            await session.execute(
                text(_STALLED_SHIPMENT_SQL),
                {"tid": tenant_id, "stalled_hours": stalled_hours},
            )
        ).all()
        pending = await self._pending_targets(session, tenant_id, spec.template_code)
        outcome = RuleOutcome(spec=spec, matched_count=0)
        for row in rows:
            shipment_id = row[0]
            order_id = row[1]
            tracking_number = str(row[2])
            carrier_code = str(row[3]) if row[3] else None
            status = str(row[4])
            stalled_since = row[5]
            order_no = str(row[6])
            outcome.matched_count += 1
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "shipment_id": str(shipment_id),
                        "order_no": order_no,
                        "tracking_number": tracking_number,
                        "standardized_status": status,
                        "stalled_since": stalled_since.isoformat(),
                        "stalled_hours": stalled_hours,
                    }
                )
            if shipment_id in pending:
                continue
            outcome.proposals.append(
                ProposalDraft(
                    capability="order_logistics",
                    proposal_type="follow_up_suggestion",
                    target_entity_type="shipment",
                    target_entity_id=shipment_id,
                    proposed_payload={
                        "rule": spec.template_code,
                        "order_id": str(order_id),
                        "order_no": order_no,
                        "tracking_number": tracking_number,
                        "carrier_code": carrier_code,
                        "standardized_status": status,
                        "stalled_since": stalled_since.isoformat(),
                        "stalled_hours": stalled_hours,
                        "recommended_action": "send_reassurance_reply_and_check_carrier",
                        "summary_zh": f"{order_no} 的货件 {tracking_number} 已 {stalled_hours} "
                        f"小时无新节点（状态 {status}），建议安抚客户并催查承运商。",
                        "summary_en": f"Shipment {tracking_number} of {order_no} has been "
                        f"silent for {stalled_hours}h (status {status}); reassure the buyer "
                        f"and chase the carrier.",
                        "grounding": {
                            "source": "sql",
                            "tables": ["shipments", "orders"],
                        },
                    },
                    citations=(
                        {
                            "kind": "sql_row",
                            "table": "shipments",
                            "shipment_id": str(shipment_id),
                        },
                    ),
                    confidence_score=Decimal("0.880"),
                )
            )
        if outcome.matched_count:
            sample = "、".join(
                f"{item['tracking_number']}({item['standardized_status']})"
                for item in outcome.items_preview
            )
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.stalled_shipment_soothe",
                    title=f"停滞货件：{outcome.matched_count} 票超过 {stalled_hours} 小时 / "
                    f"{outcome.matched_count} stalled shipment(s)",
                    body=f"超过 {stalled_hours} 小时无新物流节点的货件：{sample}。"
                    f"\nShipments silent for more than {stalled_hours}h: {sample}.",
                    priority=spec.priority,
                    deep_link="/orders",
                )
            )
        return outcome

    async def _evaluate_cold_quotes(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        cold_hours = _config_int(spec.config_json, "cold_hours", 72)
        rows = (
            await session.execute(
                text(_COLD_QUOTE_SQL), {"tid": tenant_id, "cold_hours": cold_hours}
            )
        ).all()
        pending = await self._pending_targets(session, tenant_id, spec.template_code)
        outcome = RuleOutcome(spec=spec, matched_count=0)
        for row in rows:
            quote_id = row[0]
            quote_no = str(row[1])
            currency = str(row[2]).strip()
            total_amount = Decimal(row[3])
            created_at = row[4]
            opportunity_id = row[5]
            customer_name = str(row[6]) if row[6] else None
            outcome.matched_count += 1
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "quote_id": str(quote_id),
                        "quote_no": quote_no,
                        "currency": currency,
                        "total_amount": _decimal_text(total_amount),
                        "created_at": created_at.isoformat(),
                        "customer_name": customer_name,
                        "cold_hours": cold_hours,
                    }
                )
            if quote_id in pending:
                continue
            outcome.proposals.append(
                ProposalDraft(
                    capability="sales_quoting",
                    proposal_type="follow_up_suggestion",
                    target_entity_type="quote",
                    target_entity_id=quote_id,
                    proposed_payload={
                        "rule": spec.template_code,
                        "quote_id": str(quote_id),
                        "quote_no": quote_no,
                        "opportunity_id": str(opportunity_id),
                        "customer_name": customer_name,
                        "currency": currency,
                        "total_amount": _decimal_text(total_amount),
                        "created_at": created_at.isoformat(),
                        "cold_hours": cold_hours,
                        "recommended_action": "send_b2b_follow_up_message",
                        "summary_zh": f"报价 {quote_no}"
                        f"（{currency} {_decimal_text(total_amount)}）"
                        f"已发出超过 {cold_hours} 小时仍未转化，"
                        f"建议跟进{customer_name or '客户'}。",
                        "summary_en": f"Quote {quote_no} "
                        f"({currency} {_decimal_text(total_amount)}) was sent more than "
                        f"{cold_hours}h ago and never converted; follow up with "
                        f"{customer_name or 'the buyer'}.",
                        "grounding": {
                            "source": "sql",
                            "tables": ["quotes", "opportunities", "customers"],
                        },
                    },
                    citations=(
                        {
                            "kind": "sql_row",
                            "table": "quotes",
                            "quote_id": str(quote_id),
                        },
                    ),
                    confidence_score=Decimal("0.860"),
                )
            )
        if outcome.matched_count:
            sample = "、".join(
                f"{item['quote_no']}({item['currency']} {item['total_amount']})"
                for item in outcome.items_preview
            )
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.cold_b2b_quote_followup",
                    title=f"沉睡报价：{outcome.matched_count} 张超过 {cold_hours} 小时未转化 / "
                    f"{outcome.matched_count} cold quote(s)",
                    body=f"已发送超过 {cold_hours} 小时仍未转化的报价：{sample}。"
                    f"\nQuotes sent more than {cold_hours}h ago without conversion: {sample}.",
                    priority=spec.priority,
                    deep_link="/b2b/quotes",
                )
            )
        return outcome

    async def _evaluate_patrol_brief(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        brief_hour = _config_int(spec.config_json, "brief_hour_utc", 1)
        brief = await self.workforce().daily_patrol_brief(
            session,
            tenant_id,
            # Owner scope: the brief is a system digest, never an agent-side channel.
            role=UserRole.OWNER,
            user_id=tenant_id,
        )
        outcome = RuleOutcome(spec=spec, matched_count=len(brief.alerts))
        for alert in brief.alerts[:5]:
            outcome.items_preview.append(
                {
                    "code": alert.code,
                    "severity": alert.severity,
                    "count": alert.count,
                    "title_zh": alert.title_zh,
                    "title_en": alert.title_en,
                }
            )
        outcome.notifications.append(
            NotificationDraft(
                event_type="automation.daily_patrol_brief",
                title=f"每日巡检简报 / Daily patrol brief · 健康分 {brief.health_score}",
                body=(
                    f"{brief.summary_zh}\n\n{brief.summary_en}\n\n"
                    f"计划发送 UTC 小时：{brief_hour} / scheduled UTC hour: {brief_hour}"
                ),
                priority=spec.priority,
                deep_link="/ai/employees",
            )
        )
        return outcome

    async def _evaluate_negative_reviews(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        """Turn 1-star / complaint / defect inbound messages into L1 service tickets."""
        lookback_hours = _config_int(spec.config_json, "lookback_hours", 72)
        keywords = self._review_keywords(spec.config_json)
        rows = (
            await session.execute(
                text(_NEGATIVE_REVIEW_MESSAGES_SQL),
                {
                    "tid": tenant_id,
                    "lookback_hours": lookback_hours,
                    "row_limit": _REVIEW_SCAN_LIMIT,
                },
            )
        ).all()
        pending = await self._pending_targets(session, tenant_id, spec.template_code)
        outcome = RuleOutcome(spec=spec, matched_count=0)
        severe_hit = False
        for row in rows:
            message_id = row[0]
            conversation_id = row[1]
            customer_id = row[2]
            channel_type = str(row[3])
            body_text = str(row[4])
            source_timestamp = row[5]
            customer_name = str(row[6]) if row[6] else None
            customer_email = str(row[7]) if row[7] else None
            matched = _match_review_keywords(body_text, keywords)
            if not matched:
                continue
            outcome.matched_count += 1
            severity = (
                "P0" if _SEVERE_REVIEW_KEYWORDS.intersection(matched) else "P1"
            )
            severe_hit = severe_hit or severity == "P0"
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "message_id": str(message_id),
                        "conversation_id": str(conversation_id),
                        "channel_type": channel_type,
                        "customer_name": customer_name,
                        "matched_keywords": matched,
                        "severity": severity,
                        "excerpt": _review_excerpt(body_text),
                        "received_at": source_timestamp.isoformat(),
                    }
                )
            if message_id in pending:
                continue
            excerpt = _review_excerpt(body_text)
            idempotency_key = f"{tenant_id}:{spec.template_code}:{message_id}"
            outcome.proposals.append(
                ProposalDraft(
                    capability="cs_support",
                    # The only service-shaped token allowed by the `ai_proposals` CHECK
                    # constraint is `follow_up_suggestion`; the service-ticket semantics
                    # travel in `proposed_payload.category`.
                    proposal_type="follow_up_suggestion",
                    target_entity_type="message",
                    target_entity_id=message_id,
                    proposed_payload={
                        "rule": spec.template_code,
                        "category": "service_ticket",
                        "idempotency_key": idempotency_key,
                        "message_id": str(message_id),
                        "conversation_id": str(conversation_id),
                        "customer_id": str(customer_id) if customer_id else None,
                        "customer_name": customer_name,
                        "customer_email": customer_email,
                        "channel_type": channel_type,
                        "matched_keywords": matched,
                        "severity": severity,
                        "excerpt": excerpt,
                        "received_at": source_timestamp.isoformat(),
                        "recommended_action": "open_service_ticket_and_reply",
                        "summary_zh": f"客户 {customer_name or '客户'} 在 {channel_type} "
                        f"收到负面反馈（命中：{'、'.join(matched)}），建议立即开工单并安抚回复。",
                        "summary_en": f"Negative feedback from {customer_name or 'the buyer'} "
                        f"on {channel_type} (matched: {', '.join(matched)}); open a service "
                        "ticket and reply with a remedy.",
                        "grounding": {
                            "source": "sql",
                            "tables": ["messages", "customers"],
                        },
                    },
                    citations=(
                        {
                            "kind": "sql_row",
                            "table": "messages",
                            "message_id": str(message_id),
                        },
                    ),
                    confidence_score=Decimal("0.840"),
                )
            )
        if outcome.matched_count:
            sample = "、".join(
                f"{item['customer_name'] or item['channel_type']}({item['severity']})"
                for item in outcome.items_preview
            )
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.negative_review_ticket",
                    title=f"差评/投诉：{outcome.matched_count} 条负面反馈待处理 / "
                    f"{outcome.matched_count} negative review(s)",
                    body=(
                        f"近 {lookback_hours} 小时命中负面关键词的客户消息：{sample}。"
                        f"\nInbound messages from the last {lookback_hours}h that read like "
                        f"a complaint: {sample}."
                    ),
                    priority="P0" if severe_hit else spec.priority,
                    deep_link="/inbox",
                )
            )
        return outcome

    @staticmethod
    def _review_keywords(config: dict[str, Any]) -> tuple[str, ...]:
        raw = config.get("keywords")
        if not isinstance(raw, list) or not raw:
            return DEFAULT_NEGATIVE_REVIEW_KEYWORDS
        cleaned = tuple(
            str(item).strip().lower()
            for item in raw
            if isinstance(item, str) and str(item).strip()
        )
        return cleaned or DEFAULT_NEGATIVE_REVIEW_KEYWORDS

    async def _evaluate_high_adoption_faqs(
        self, session: AsyncSession, tenant_id: UUID, spec: RuleSpec
    ) -> RuleOutcome:
        """Turn approved high-confidence replies into structured FAQ extraction proposals."""
        # Confidence is a 3-decimal NUMERIC(4, 3); quantize here so the SQL comparison
        # and the notification copy agree with the seeded `0.900` default.
        min_confidence = _config_decimal(
            spec.config_json, "min_confidence", Decimal("0.900")
        ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        lookback_days = _config_int(spec.config_json, "lookback_days", 30)
        rows = (
            await session.execute(
                text(_HIGH_ADOPTION_REPLIES_SQL),
                {
                    "tid": tenant_id,
                    "min_confidence": min_confidence,
                    "lookback_days": lookback_days,
                    "promotion_prefix": FAQ_PROMOTION_URI_PREFIX,
                    "row_limit": _REVIEW_SCAN_LIMIT,
                },
            )
        ).all()
        pending = await self._pending_targets(session, tenant_id, spec.template_code)
        outcome = RuleOutcome(spec=spec, matched_count=0)
        for row in rows:
            proposal_id = row[0]
            conversation_id = row[1]
            status = str(row[2])
            confidence = Decimal(str(row[3]))
            proposed_payload = row[4] if isinstance(row[4], dict) else {}
            edited_payload = row[5] if isinstance(row[5], dict) else None
            question = str(row[7]).strip() if row[7] else ""
            answer_payload = edited_payload or proposed_payload
            answer = str(answer_payload.get("body_text") or "").strip()
            if not question or not answer:
                # Nothing to extract without both halves of the Q&A pair.
                continue
            outcome.matched_count += 1
            if len(outcome.items_preview) < 5:
                outcome.items_preview.append(
                    {
                        "proposal_id": str(proposal_id),
                        "conversation_id": str(conversation_id),
                        "status": status,
                        "confidence_score": format(confidence, "f"),
                        "question": _review_excerpt(question),
                        "answer": _review_excerpt(answer),
                    }
                )
            if proposal_id in pending:
                continue
            idempotency_key = f"{tenant_id}:{spec.template_code}:{proposal_id}"
            outcome.proposals.append(
                ProposalDraft(
                    capability="cs_support",
                    proposal_type="crm_entity_extract",
                    target_entity_type="ai_proposal",
                    target_entity_id=proposal_id,
                    proposed_payload={
                        "rule": spec.template_code,
                        "category": "faq_extract",
                        "idempotency_key": idempotency_key,
                        "source_proposal_id": str(proposal_id),
                        "source_proposal_status": status,
                        "conversation_id": str(conversation_id),
                        "confidence_score": format(confidence, "f"),
                        "question": question,
                        "answer": answer,
                        "promotion_marker": f"{FAQ_PROMOTION_URI_PREFIX}{proposal_id}",
                        "recommended_action": "promote_to_kb_faq",
                        "summary_zh": f"将已采纳的 AI 回复（置信度 "
                        f"{format(confidence, 'f')}）转为知识库 FAQ：{_review_excerpt(question)}",
                        "summary_en": f"Promote the approved AI reply (confidence "
                        f"{format(confidence, 'f')}) into a knowledge-base FAQ: "
                        f"{_review_excerpt(question)}",
                        "grounding": {
                            "source": "sql",
                            "tables": ["ai_proposals", "messages", "kb_documents"],
                        },
                    },
                    citations=(
                        {
                            "kind": "sql_row",
                            "table": "ai_proposals",
                            "proposal_id": str(proposal_id),
                        },
                    ),
                    confidence_score=confidence.quantize(
                        Decimal("0.001"), rounding=ROUND_HALF_UP
                    ),
                )
            )
        if outcome.matched_count:
            outcome.notifications.append(
                NotificationDraft(
                    event_type="automation.high_adoption_faq_extract",
                    title=f"知识库沉淀：{outcome.matched_count} 条已采纳回复可转 FAQ / "
                    f"{outcome.matched_count} reply(ies) ready as FAQ",
                    body=(
                        f"置信度 ≥ {format(min_confidence, 'f')} 且尚未入库的已采纳回复："
                        f"{outcome.matched_count} 条，建议确认后写入知识库。"
                        f"\nApproved replies at or above confidence "
                        f"{format(min_confidence, 'f')} that are not in the KB yet: "
                        f"{outcome.matched_count}."
                    ),
                    priority=spec.priority,
                    deep_link="/ai/employees",
                )
            )
        return outcome

    # -- writes -------------------------------------------------------------
    async def _apply(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        outcome: RuleOutcome,
        notification_service: NotificationService,
    ) -> None:
        for proposal in outcome.proposals:
            inserted = await session.scalar(
                text(_INSERT_PROPOSAL_SQL),
                {
                    "tid": tenant_id,
                    "capability": proposal.capability,
                    "proposal_type": proposal.proposal_type,
                    "target_entity_type": proposal.target_entity_type,
                    "target_entity_id": proposal.target_entity_id,
                    "payload": _json_dumps(proposal.proposed_payload),
                    "citations": _json_dumps(list(proposal.citations)),
                    "confidence_score": proposal.confidence_score,
                    "rule_key": _RULE_PAYLOAD_KEY,
                    "rule": outcome.spec.template_code,
                },
            )
            if inserted is not None:
                outcome.proposals_created += 1
        for notification in outcome.notifications:
            deliveries = await notification_service.queue_delivery(
                session,
                tenant_id,
                event_type=notification.event_type,
                title=notification.title,
                body=notification.body,
                priority=notification.priority,
                deep_link=notification.deep_link,
            )
            outcome.notifications_queued += len(deliveries)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
