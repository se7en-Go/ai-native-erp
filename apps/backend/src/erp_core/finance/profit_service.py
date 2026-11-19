from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import OrderProfitSnapshot, PaymentRecord, ReceivablePayable
from erp_core.finance.schemas import OrderProfitSnapshotRead, ProfitSummaryRead
from erp_core.shared.enums import OrderMode
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event


def _q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _metadata_decimal(metadata: dict[str, Any], key: str) -> Decimal:
    value = metadata.get(key, "0")
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise AppError(
            slug="invalid-finance-metadata",
            status_code=422,
            code="ERR_INVALID_FINANCE_METADATA",
            title="Invalid Financial Metadata",
            detail=f"{key} must be an exact decimal value.",
        )
    return Decimal(value)


class OrderProfitService:
    """Decimal-exact CVP contribution margin snapshot calculator and aggregator."""

    @staticmethod
    def _to_read(snap: OrderProfitSnapshot) -> OrderProfitSnapshotRead:
        gross = Decimal(str(snap.gross_revenue_base))
        net = Decimal(str(snap.net_contribution_margin_base))
        margin_pct = Decimal("0.00") if gross <= Decimal("0") else _q2(net / gross * Decimal("100"))
        return OrderProfitSnapshotRead(
            order_id=snap.order_id,
            tenant_id=snap.tenant_id,
            base_currency=snap.base_currency,
            gross_revenue_base=gross,
            cogs_base=Decimal(str(snap.cogs_base)),
            platform_commission_base=Decimal(str(snap.platform_commission_base)),
            payment_gateway_fee_base=Decimal(str(snap.payment_gateway_fee_base)),
            shipping_and_duty_base=Decimal(str(snap.shipping_and_duty_base)),
            channel_msg_cost_base=Decimal(str(snap.channel_msg_cost_base)),
            ad_attribution_cost_base=Decimal(str(snap.ad_attribution_cost_base)),
            fx_gain_loss_base=Decimal(str(snap.fx_gain_loss_base)),
            net_contribution_margin_base=net,
            contribution_margin_pct=margin_pct,
            recalculated_at=snap.recalculated_at,
        )

    async def recalculate_order_profit(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        platform_commission_base: Decimal | None = None,
        channel_msg_cost_base: Decimal | None = None,
        ad_attribution_cost_base: Decimal | None = None,
    ) -> OrderProfitSnapshotRead:
        order_row = (
            await session.execute(
                text(
                    """
                    SELECT
                        o.id, o.total_amount, o.fx_rate_to_base, o.fulfillment_status,
                        o.shipping_charged, o.channel_metadata, t.base_currency
                    FROM orders o
                    JOIN tenants t ON t.id = o.tenant_id
                    WHERE o.tenant_id = :tid AND o.id = :oid FOR UPDATE OF o
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).one_or_none()
        if order_row is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )

        existing = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id,
                    OrderProfitSnapshot.order_id == order_id,
                )
            )
        ).scalar_one_or_none()

        locked_fx = Decimal(str(order_row.fx_rate_to_base))
        order_total = Decimal(str(order_row.total_amount))
        metadata = dict(order_row.channel_metadata or {})
        refunded_ccy = _metadata_decimal(metadata, "refunded_amount")

        if order_row.fulfillment_status == "cancelled":
            gross_revenue_base = Decimal("0.00")
            cogs_base = Decimal("0.00")
        else:
            net_ccy = max(Decimal("0.00"), order_total - refunded_ccy)
            gross_revenue_base = _q2(net_ccy * locked_fx)
            cogs_raw = (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(quantity * unit_cost_base_snapshot), 0)
                        FROM order_items
                        WHERE tenant_id = :tid AND order_id = :oid
                        """
                    ),
                    {"tid": tenant_id, "oid": order_id},
                )
            ).scalar_one()
            cogs_base = _q2(Decimal(str(cogs_raw)))

        ship_raw = (
            await session.execute(
                text(
                    """
                    SELECT SUM(actual_shipping_cost_base)
                    FROM shipments
                    WHERE tenant_id = :tid AND order_id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).scalar_one()
        shipping_and_duty_base = _q2(
            Decimal(ship_raw) if ship_raw is not None else order_row.shipping_charged * locked_fx
        )

        payment_rows = (
            (
                await session.execute(
                    select(PaymentRecord)
                    .join(ReceivablePayable, ReceivablePayable.id == PaymentRecord.rp_id)
                    .where(
                        PaymentRecord.tenant_id == tenant_id,
                        PaymentRecord.verified_by_user_id.is_not(None),
                        ReceivablePayable.tenant_id == tenant_id,
                        ReceivablePayable.order_id == order_id,
                        ReceivablePayable.direction == "receivable",
                    )
                )
            )
            .scalars()
            .all()
        )

        gateway_fee_base = Decimal("0.00")
        fx_gain_loss_base = Decimal("0.00")
        for pr in payment_rows:
            amt = Decimal(str(pr.amount_received))
            actual_fx = Decimal(str(pr.actual_fx_rate))
            fee = Decimal(str(pr.gateway_fee))
            gateway_fee_base += fee * actual_fx
            fx_gain_loss_base += amt * (actual_fx - locked_fx)

        gateway_fee_base = _q2(gateway_fee_base)
        fx_gain_loss_base = _q2(fx_gain_loss_base)

        comm_base = _q2(
            platform_commission_base
            if platform_commission_base is not None
            else (
                Decimal(str(existing.platform_commission_base))
                if existing is not None
                else _metadata_decimal(metadata, "platform_commission_base")
            )
        )
        msg_base = _q2(
            channel_msg_cost_base
            if channel_msg_cost_base is not None
            else (
                Decimal(str(existing.channel_msg_cost_base))
                if existing is not None
                else Decimal("0.00")
            )
        )
        ad_base = _q2(
            ad_attribution_cost_base
            if ad_attribution_cost_base is not None
            else (
                Decimal(str(existing.ad_attribution_cost_base))
                if existing is not None
                else _metadata_decimal(metadata, "ad_attribution_cost_base")
            )
        )

        now = datetime.now(UTC)
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO order_profit_snapshots (
                        order_id, tenant_id, base_currency, gross_revenue_base, cogs_base,
                        platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base,
                        channel_msg_cost_base, ad_attribution_cost_base, fx_gain_loss_base,
                        recalculated_at
                    ) VALUES (
                        :oid, :tid, :bccy, :gross, :cogs, :comm, :gw, :ship, :msg, :ad, :fx, :ts
                    )
                    ON CONFLICT (order_id) DO UPDATE SET
                        base_currency = EXCLUDED.base_currency,
                        gross_revenue_base = EXCLUDED.gross_revenue_base,
                        cogs_base = EXCLUDED.cogs_base,
                        platform_commission_base = EXCLUDED.platform_commission_base,
                        payment_gateway_fee_base = EXCLUDED.payment_gateway_fee_base,
                        shipping_and_duty_base = EXCLUDED.shipping_and_duty_base,
                        channel_msg_cost_base = EXCLUDED.channel_msg_cost_base,
                        ad_attribution_cost_base = EXCLUDED.ad_attribution_cost_base,
                        fx_gain_loss_base = EXCLUDED.fx_gain_loss_base,
                        recalculated_at = EXCLUDED.recalculated_at
                    RETURNING order_id
                    """
                ),
                {
                    "oid": order_id,
                    "tid": tenant_id,
                    "bccy": order_row.base_currency or "CNY",
                    "gross": gross_revenue_base,
                    "cogs": cogs_base,
                    "comm": comm_base,
                    "gw": gateway_fee_base,
                    "ship": shipping_and_duty_base,
                    "msg": msg_base,
                    "ad": ad_base,
                    "fx": fx_gain_loss_base,
                    "ts": now,
                },
            )
        ).one()
        await session.flush()

        refreshed = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id,
                    OrderProfitSnapshot.order_id == row.order_id,
                )
            )
        ).scalar_one()
        await session.refresh(refreshed)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="finance.profit_service",
            action="finance.profit_snapshot.recalculated",
            resource_type="order",
            resource_id=order_id,
            after_state={
                "gross_revenue_base": str(refreshed.gross_revenue_base),
                "cogs_base": str(refreshed.cogs_base),
                "net_contribution_margin_base": str(refreshed.net_contribution_margin_base),
            },
        )
        return self._to_read(refreshed)

    async def get_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> OrderProfitSnapshotRead:
        snap = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id,
                    OrderProfitSnapshot.order_id == order_id,
                )
            )
        ).scalar_one_or_none()
        if snap is None:
            return await self.recalculate_order_profit(session, tenant_id, order_id)
        return self._to_read(snap)

    async def get_profit_summary(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_mode: OrderMode | None = None,
        channel_account_id: UUID | None = None,
    ) -> ProfitSummaryRead:
        clauses = ["s.tenant_id = :tid"]
        params: dict[str, object] = {"tid": tenant_id}
        if order_mode is not None:
            clauses.append("o.order_mode = :omode")
            params["omode"] = order_mode.value
        if channel_account_id is not None:
            clauses.append("o.channel_account_id = :caid")
            params["caid"] = channel_account_id

        where_sql = " AND ".join(clauses)
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(MAX(s.base_currency), 'CNY') AS base_currency,
                        COUNT(*) AS total_orders,
                        COALESCE(SUM(s.gross_revenue_base), 0) AS gross,
                        COALESCE(SUM(s.cogs_base), 0) AS cogs,
                        COALESCE(SUM(
                            s.platform_commission_base + s.payment_gateway_fee_base
                            + s.shipping_and_duty_base + s.channel_msg_cost_base
                            + s.ad_attribution_cost_base
                        ), 0) AS variable_expenses,
                        COALESCE(SUM(s.fx_gain_loss_base), 0) AS fx_gain_loss,
                        COALESCE(SUM(s.net_contribution_margin_base), 0) AS net_margin
                    FROM order_profit_snapshots s
                    JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                    WHERE {where_sql}
                    """
                ),
                params,
            )
        ).one()

        gross = _q2(Decimal(str(row.gross)))
        net = _q2(Decimal(str(row.net_margin)))
        pct = Decimal("0.00") if gross <= Decimal("0") else _q2(net / gross * Decimal("100"))
        return ProfitSummaryRead(
            base_currency=str(row.base_currency),
            total_orders=int(row.total_orders),
            gross_revenue_base=gross,
            cogs_base=_q2(Decimal(str(row.cogs))),
            total_variable_expenses_base=_q2(Decimal(str(row.variable_expenses))),
            fx_gain_loss_base=_q2(Decimal(str(row.fx_gain_loss))),
            net_contribution_margin_base=net,
            contribution_margin_pct=pct,
            order_mode=order_mode,
        )
