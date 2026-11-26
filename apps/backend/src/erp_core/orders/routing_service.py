"""Order routing and anti-loss hold engine.

The routing decision is intentionally self-contained inside ``erp_core.orders``:

* ``orders`` must never import ``erp_core.ai_engine`` (import-linter
  ``domain_modules_never_import_ai_engine``), so nothing here touches AI modules.
* Profit data (``order_profit_snapshots``) and warehouse lookups are read through
  parameterized SQLAlchemy ``text(...)`` queries instead of ORM relationships, which
  keeps the finance schema out of the orders domain graph while staying 100% clean
  under ``infra/importlinter.ini``.

Every margin computation stays in ``decimal.Decimal`` with ``ROUND_HALF_UP`` and is
persisted into ``NUMERIC(8, 4)`` - never an IEEE-754 float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.orders.models import Order
from erp_core.orders.routing_models import OrderRoutingEvaluation, OrderRoutingRule
from erp_core.orders.routing_schemas import (
    RoutingActions,
    RoutingConditions,
    RoutingRuleCreate,
    RoutingRuleRead,
    RoutingRuleUpdate,
)
from erp_core.shared.enums import OrderMode, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

#: Four-decimal precision of ``order_routing_evaluations.estimated_margin_pct``.
_MARGIN_QUANTUM = Decimal("0.0001")
#: Thousand-gram-per-kilogram divisor as an exact Decimal.
_GRAMS_PER_KG = Decimal(1000)

#: Built-in anti-loss guard: an order whose contribution margin is negative is held for
#: review even when no tenant rule says so.
DEFAULT_HOLD_MARGIN_PCT = Decimal("0")

_WRITER_ROLES = frozenset({UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER})
_HOLD_RELEASE_ROLES = _WRITER_ROLES

#: ``orders.channel_metadata`` keys that carry the shipping destination.
_DESTINATION_METADATA_KEYS: tuple[str, ...] = (
    "destination_country",
    "shipping_country",
    "destination_country_code",
    "country_code",
)


def quantize_margin(value: Decimal) -> Decimal:
    """Round a margin percentage to the persisted 4-decimal precision."""
    return value.quantize(_MARGIN_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class OrderFacts:
    """Everything the rules engine needs about one order, already normalized."""

    order_id: UUID
    order_no: str
    order_mode: str
    destination_country: str | None
    channel_type: str | None
    total_amount: Decimal
    total_weight_kg: Decimal
    gross_revenue_base: Decimal
    estimated_margin_pct: Decimal | None


@dataclass(frozen=True)
class RoutingOutcome:
    """Pure (pre-persistence) result of evaluating the enabled rules."""

    matched_rule_ids: list[str]
    recommended_warehouse_id: UUID | None
    recommended_carrier: str | None
    recommended_service: str | None
    applied_tags: list[str]
    is_held: bool
    hold_reason: str | None


def _rule_matches(conditions: RoutingConditions, facts: OrderFacts) -> bool:
    """Whether the rule's *scope* (not its margin floor) applies to this order."""
    if conditions.destination_countries:
        country = (facts.destination_country or "").upper()
        if country not in conditions.destination_countries:
            return False
    if conditions.channel_types:
        channel = (facts.channel_type or "").lower()
        if channel not in conditions.channel_types:
            return False
    if conditions.order_modes and facts.order_mode not in conditions.order_modes:
        return False
    if (
        conditions.min_total_amount is not None
        and facts.total_amount < conditions.min_total_amount
    ):
        return False
    if conditions.max_weight_kg is not None:
        return not facts.total_weight_kg > conditions.max_weight_kg
    return True


def _format_pct(value: Decimal) -> str:
    return f"{value.normalize():f}"


def evaluate_rules(
    rules: list[OrderRoutingRule],
    facts: OrderFacts,
) -> RoutingOutcome:
    """Apply enabled rules (ascending ``priority``) to one order.

    Higher-priority rules (lower ``priority`` number) win the recommendation fields;
    the first hold that triggers wins the hold reason.
    """
    matched_ids: list[str] = []
    recommended_warehouse_id: UUID | None = None
    recommended_carrier: str | None = None
    recommended_service: str | None = None
    tags: list[str] = []
    is_held = False
    hold_reason: str | None = None

    for rule in rules:
        conditions = RoutingConditions.model_validate(rule.conditions_json or {})
        if not _rule_matches(conditions, facts):
            continue
        matched_ids.append(str(rule.id))
        actions = RoutingActions.model_validate(rule.actions_json or {})

        if recommended_warehouse_id is None and actions.warehouse_id is not None:
            recommended_warehouse_id = actions.warehouse_id
        if recommended_carrier is None and actions.carrier_code:
            recommended_carrier = actions.carrier_code
        if recommended_service is None and actions.service_code:
            recommended_service = actions.service_code
        for tag in actions.add_tags:
            if tag not in tags:
                tags.append(tag)

        floor = conditions.min_margin_pct
        margin = facts.estimated_margin_pct
        if floor is not None and margin is not None and margin < floor and not is_held:
            is_held = True
            hold_reason = actions.hold_reason or (
                f"Estimated margin {_format_pct(margin)}% is below the "
                f"{_format_pct(floor)}% floor on rule '{rule.name}'"
            )
        if actions.hold_for_review and not is_held:
            is_held = True
            hold_reason = actions.hold_reason or (
                f"Rule '{rule.name}' holds orders for manual review"
            )

    margin = facts.estimated_margin_pct
    if not is_held and margin is not None and margin < DEFAULT_HOLD_MARGIN_PCT:
        is_held = True
        hold_reason = (
            f"Estimated margin {_format_pct(margin)}% is negative "
            "(built-in anti-loss guard)"
        )

    return RoutingOutcome(
        matched_rule_ids=matched_ids,
        recommended_warehouse_id=recommended_warehouse_id,
        recommended_carrier=recommended_carrier,
        recommended_service=recommended_service,
        applied_tags=tags,
        is_held=is_held,
        hold_reason=hold_reason,
    )


class RoutingService:
    """CRUD for routing rules plus the evaluation / hold-release workflow."""

    # -- rule CRUD ---------------------------------------------------------------

    async def list_rules(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[RoutingRuleRead]:
        rules = (
            await session.execute(
                select(OrderRoutingRule)
                .where(OrderRoutingRule.tenant_id == tenant_id)
                .order_by(OrderRoutingRule.priority.asc(), OrderRoutingRule.created_at.asc())
            )
        ).scalars()
        return [self._read_rule(rule) for rule in rules]

    async def _require_warehouse(
        self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID | None
    ) -> None:
        if warehouse_id is None:
            return
        found = await session.scalar(
            text(
                "SELECT id FROM warehouses "
                "WHERE tenant_id = :tid AND id = :wid AND is_active = true"
            ),
            {"tid": tenant_id, "wid": warehouse_id},
        )
        if found is None:
            raise AppError(
                slug="routing-warehouse-not-found",
                status_code=422,
                code="ERR_ROUTING_WAREHOUSE_NOT_FOUND",
                title="Routing Warehouse Not Found",
                detail=f"Warehouse '{warehouse_id}' is not an active warehouse of this tenant.",
            )

    @staticmethod
    def _read_rule(rule: OrderRoutingRule) -> RoutingRuleRead:
        return RoutingRuleRead(
            id=rule.id,
            name=rule.name,
            priority=rule.priority,
            is_enabled=rule.is_enabled,
            conditions=RoutingConditions.model_validate(rule.conditions_json or {}),
            actions=RoutingActions.model_validate(rule.actions_json or {}),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def create_rule(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: RoutingRuleCreate,
    ) -> RoutingRuleRead:
        self._require_writer(principal)
        await self._require_warehouse(session, tenant_id, payload.actions.warehouse_id)
        rule = OrderRoutingRule(
            tenant_id=tenant_id,
            name=payload.name,
            priority=payload.priority,
            is_enabled=payload.is_enabled,
            conditions_json=payload.conditions.to_jsonb(),
            actions_json=payload.actions.to_jsonb(),
        )
        session.add(rule)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="order_routing_rule.created",
            resource_type="order_routing_rule",
            resource_id=rule.id,
            after_state={
                "name": rule.name,
                "priority": rule.priority,
                "conditions": rule.conditions_json,
                "actions": rule.actions_json,
            },
        )
        return self._read_rule(rule)

    async def update_rule(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        rule_id: UUID,
        payload: RoutingRuleUpdate,
    ) -> RoutingRuleRead:
        self._require_writer(principal)
        rule = await self._load_rule(session, tenant_id, rule_id)
        before: dict[str, Any] = {
            "name": rule.name,
            "priority": rule.priority,
            "is_enabled": rule.is_enabled,
            "conditions": rule.conditions_json,
            "actions": rule.actions_json,
        }
        if payload.actions is not None:
            await self._require_warehouse(session, tenant_id, payload.actions.warehouse_id)
        if payload.name is not None:
            rule.name = payload.name.strip()
        if payload.priority is not None:
            rule.priority = payload.priority
        if payload.is_enabled is not None:
            rule.is_enabled = payload.is_enabled
        if payload.conditions is not None:
            rule.conditions_json = payload.conditions.to_jsonb()
        if payload.actions is not None:
            rule.actions_json = payload.actions.to_jsonb()
        rule.updated_at = datetime.now(UTC)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="order_routing_rule.updated",
            resource_type="order_routing_rule",
            resource_id=rule.id,
            before_state=before,
            after_state={
                "name": rule.name,
                "priority": rule.priority,
                "is_enabled": rule.is_enabled,
                "conditions": rule.conditions_json,
                "actions": rule.actions_json,
            },
        )
        return self._read_rule(rule)

    async def delete_rule(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        rule_id: UUID,
    ) -> None:
        self._require_writer(principal)
        rule = await self._load_rule(session, tenant_id, rule_id)
        before = {
            "name": rule.name,
            "priority": rule.priority,
            "is_enabled": rule.is_enabled,
            "conditions": rule.conditions_json,
            "actions": rule.actions_json,
        }
        await session.delete(rule)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="order_routing_rule.deleted",
            resource_type="order_routing_rule",
            resource_id=rule_id,
            before_state=before,
        )

    async def _load_rule(
        self, session: AsyncSession, tenant_id: UUID, rule_id: UUID
    ) -> OrderRoutingRule:
        rule = await session.scalar(
            select(OrderRoutingRule).where(
                OrderRoutingRule.tenant_id == tenant_id,
                OrderRoutingRule.id == rule_id,
            )
        )
        if rule is None:
            raise AppError(
                slug="routing-rule-not-found",
                status_code=404,
                code="ERR_ROUTING_RULE_NOT_FOUND",
                title="Routing Rule Not Found",
                detail=f"Routing rule '{rule_id}' not found.",
            )
        return rule

    @staticmethod
    def _require_writer(principal: CurrentPrincipal) -> None:
        if principal.role not in _WRITER_ROLES:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Insufficient Role",
                detail="Only Owner, Admin or Manager may change routing rules.",
            )

    # -- evaluation --------------------------------------------------------------

    async def latest_evaluation(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> OrderRoutingEvaluation | None:
        return (
            await session.execute(
                select(OrderRoutingEvaluation)
                .where(
                    OrderRoutingEvaluation.tenant_id == tenant_id,
                    OrderRoutingEvaluation.order_id == order_id,
                )
                .order_by(
                    OrderRoutingEvaluation.evaluated_at.desc(),
                    OrderRoutingEvaluation.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _load_order(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> Order:
        order = (
            await session.execute(
                select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id)
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )
        return order

    @staticmethod
    def _destination_from_metadata(order: Order) -> str | None:
        metadata: dict[str, Any] = order.channel_metadata or {}
        for key in _DESTINATION_METADATA_KEYS:
            raw = metadata.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().upper()[:2]
        shipping = metadata.get("shipping_address")
        if isinstance(shipping, dict):
            raw = shipping.get("country") or shipping.get("country_code")
            if isinstance(raw, str) and raw.strip():
                return raw.strip().upper()[:2]
        return None

    async def build_order_facts(
        self, session: AsyncSession, tenant_id: UUID, order: Order
    ) -> OrderFacts:
        """Resolve destination, channel, weight and the estimated contribution margin."""
        destination = self._destination_from_metadata(order)
        if destination is None:
            country = await session.scalar(
                text("SELECT country_code FROM customers WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": order.customer_id},
            )
            if isinstance(country, str) and country.strip():
                destination = country.strip().upper()[:2]

        channel_type: str | None = None
        if order.channel_account_id is not None:
            raw_channel = await session.scalar(
                text(
                    "SELECT channel_type FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :caid"
                ),
                {"tid": tenant_id, "caid": order.channel_account_id},
            )
            if isinstance(raw_channel, str):
                channel_type = raw_channel

        weight_grams = await session.scalar(
            text(
                "SELECT COALESCE(SUM(v.weight_grams * i.quantity), 0) "
                "FROM order_items i "
                "JOIN product_variants v "
                "  ON v.id = i.variant_id AND v.tenant_id = i.tenant_id "
                "WHERE i.tenant_id = :tid AND i.order_id = :oid"
            ),
            {"tid": tenant_id, "oid": order.id},
        )
        total_weight_kg = (Decimal(str(weight_grams or 0)) / _GRAMS_PER_KG).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )

        snapshot = (
            await session.execute(
                text(
                    "SELECT gross_revenue_base, net_contribution_margin_base "
                    "FROM order_profit_snapshots "
                    "WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant_id, "oid": order.id},
            )
        ).first()
        gross_revenue_base = Decimal("0.00")
        margin_pct: Decimal | None = None
        if snapshot is not None:
            gross_revenue_base = Decimal(str(snapshot[0] or "0.00"))
            net_margin = Decimal(str(snapshot[1] or "0.00"))
            if gross_revenue_base > 0:
                margin_pct = quantize_margin(
                    net_margin / gross_revenue_base * Decimal(100)
                )
        if margin_pct is None:
            # No snapshot yet: fall back to the landed-cost math the order already
            # carries so anti-loss routing still works before the nightly rollup runs.
            gross_revenue_base = (
                Decimal(str(order.total_amount)) * Decimal(str(order.fx_rate_to_base))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cogs_base = await session.scalar(
                text(
                    "SELECT COALESCE(SUM(i.unit_cost_base_snapshot * i.quantity), 0) "
                    "FROM order_items i WHERE i.tenant_id = :tid AND i.order_id = :oid"
                ),
                {"tid": tenant_id, "oid": order.id},
            )
            if gross_revenue_base > 0:
                net = gross_revenue_base - Decimal(str(cogs_base or 0))
                margin_pct = quantize_margin(net / gross_revenue_base * Decimal(100))

        return OrderFacts(
            order_id=order.id,
            order_no=order.order_no,
            order_mode=order.order_mode.value
            if isinstance(order.order_mode, OrderMode)
            else str(order.order_mode),
            destination_country=destination,
            channel_type=channel_type,
            total_amount=Decimal(str(order.total_amount)),
            total_weight_kg=total_weight_kg,
            gross_revenue_base=gross_revenue_base,
            estimated_margin_pct=margin_pct,
        )

    async def evaluate_order(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> OrderRoutingEvaluation:
        """Evaluate the enabled rules for one order and persist the decision."""
        order = await self._load_order(session, tenant_id, order_id)
        facts = await self.build_order_facts(session, tenant_id, order)

        rules = list(
            (
                await session.execute(
                    select(OrderRoutingRule)
                    .where(
                        OrderRoutingRule.tenant_id == tenant_id,
                        OrderRoutingRule.is_enabled.is_(True),
                    )
                    .order_by(
                        OrderRoutingRule.priority.asc(),
                        OrderRoutingRule.created_at.asc(),
                        OrderRoutingRule.id.asc(),
                    )
                )
            ).scalars()
        )
        outcome = evaluate_rules(rules, facts)

        evaluation = OrderRoutingEvaluation(
            tenant_id=tenant_id,
            order_id=order.id,
            matched_rule_ids=outcome.matched_rule_ids,
            recommended_warehouse_id=outcome.recommended_warehouse_id,
            recommended_carrier=outcome.recommended_carrier,
            recommended_service=outcome.recommended_service,
            estimated_margin_pct=facts.estimated_margin_pct,
            is_held=outcome.is_held,
            hold_reason=outcome.hold_reason,
            applied_tags=outcome.applied_tags,
        )
        session.add(evaluation)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="order-routing-engine",
            action="order.routing_evaluated",
            resource_type="order",
            resource_id=order.id,
            after_state={
                "evaluation_id": str(evaluation.id),
                "matched_rule_ids": outcome.matched_rule_ids,
                "estimated_margin_pct": (
                    _format_pct(facts.estimated_margin_pct)
                    if facts.estimated_margin_pct is not None
                    else None
                ),
                "is_held": outcome.is_held,
                "hold_reason": outcome.hold_reason,
                "recommended_carrier": outcome.recommended_carrier,
                "recommended_service": outcome.recommended_service,
                "recommended_warehouse_id": (
                    str(outcome.recommended_warehouse_id)
                    if outcome.recommended_warehouse_id
                    else None
                ),
                "applied_tags": outcome.applied_tags,
            },
        )
        return evaluation

    # -- hold release ------------------------------------------------------------

    async def release_hold(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        evaluation_or_order_id: UUID,
        reason: str,
    ) -> OrderRoutingEvaluation:
        """Clear an active anti-loss hold; Owner/Admin/Manager only."""
        if principal.role not in _HOLD_RELEASE_ROLES:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Insufficient Role",
                detail="Only Owner, Admin or Manager may release a routing hold.",
            )
        cleaned_reason = reason.strip()
        if len(cleaned_reason) < 3:
            raise AppError(
                slug="hold-release-reason-required",
                status_code=422,
                code="ERR_HOLD_RELEASE_REASON_REQUIRED",
                title="Release Reason Required",
                detail="A hold release reason of at least 3 characters is required.",
            )

        evaluation = await session.scalar(
            select(OrderRoutingEvaluation).where(
                OrderRoutingEvaluation.tenant_id == tenant_id,
                OrderRoutingEvaluation.id == evaluation_or_order_id,
            )
        )
        if evaluation is None:
            evaluation = await self.latest_evaluation(
                session, tenant_id, evaluation_or_order_id
            )
        if evaluation is None:
            raise AppError(
                slug="routing-evaluation-not-found",
                status_code=404,
                code="ERR_ROUTING_EVALUATION_NOT_FOUND",
                title="Routing Evaluation Not Found",
                detail=f"No routing evaluation found for '{evaluation_or_order_id}'.",
            )
        if not evaluation.is_held:
            raise AppError(
                slug="routing-hold-not-active",
                status_code=409,
                code="ERR_ROUTING_HOLD_NOT_ACTIVE",
                title="No Active Routing Hold",
                detail=(
                    f"Routing evaluation '{evaluation.id}' is already unheld; "
                    "there is nothing to release."
                ),
            )

        previous_reason = evaluation.hold_reason
        evaluation.is_held = False
        evaluation.hold_released_by = principal.user_id
        evaluation.hold_released_at = datetime.now(UTC)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="order.routing_hold_released",
            resource_type="order",
            resource_id=evaluation.order_id,
            before_state={"is_held": True, "hold_reason": previous_reason},
            after_state={
                "is_held": False,
                "evaluation_id": str(evaluation.id),
                "release_reason": cleaned_reason,
            },
        )
        return evaluation
