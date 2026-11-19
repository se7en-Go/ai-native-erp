"""Order contribution-margin (CVP) snapshot recomputation with `Decimal` only.

`net_contribution_margin_base` is a PostgreSQL `GENERATED ALWAYS AS ... STORED` column, so the
snapshot row is written with its component costs and the database derives the margin. All
money arithmetic here stays in `Decimal`/`NUMERIC`; no IEEE-754 floats are involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import OrderProfitSnapshot
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

ZERO = Decimal("0.00")


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class OrderProfitService:
    """Recompute and persist the CVP snapshot for a single order."""

    async def recalculate_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> OrderProfitSnapshot:
        # `orders` / `order_items` are owned by the orders module: read them through
        # parameterized RLS-scoped statements, never a cross-module ORM model import.
        order = (
            await session.execute(
                text(
                    "SELECT total_amount, fx_rate_to_base, shipping_charged FROM orders "
                    "WHERE tenant_id = :tid AND id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' does not exist.",
            )

        items = (
            await session.execute(
                text(
                    "SELECT unit_cost_base_snapshot, quantity FROM order_items "
                    "WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).all()

        fx_rate = Decimal(order.fx_rate_to_base)
        gross_revenue_base = _money(Decimal(order.total_amount) * fx_rate)
        cogs_base = sum(
            (_money(Decimal(item.unit_cost_base_snapshot) * int(item.quantity)) for item in items),
            ZERO,
        )
        cogs_base = _money(cogs_base)
        shipping_and_duty_base = _money(Decimal(order.shipping_charged) * fx_rate)

        snapshot = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id,
                    OrderProfitSnapshot.order_id == order_id,
                )
            )
        ).scalar_one_or_none()

        if snapshot is None:
            snapshot = OrderProfitSnapshot(
                order_id=order_id,
                tenant_id=tenant_id,
                base_currency="CNY",
            )
            session.add(snapshot)

        snapshot.gross_revenue_base = gross_revenue_base
        snapshot.cogs_base = cogs_base
        snapshot.shipping_and_duty_base = shipping_and_duty_base
        snapshot.recalculated_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(snapshot)

        margin = (
            await session.execute(
                text(
                    "SELECT net_contribution_margin_base FROM order_profit_snapshots "
                    "WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).scalar_one()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="finance.profit_service",
            action="finance.profit_snapshot.recalculated",
            resource_type="order",
            resource_id=order_id,
            after_state={
                "gross_revenue_base": str(gross_revenue_base),
                "cogs_base": str(cogs_base),
                "net_contribution_margin_base": str(margin),
            },
        )
        return snapshot
