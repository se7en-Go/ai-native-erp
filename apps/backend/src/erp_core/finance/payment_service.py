from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import PaymentRecord, ReceivablePayable
from erp_core.finance.profit_service import OrderProfitService
from erp_core.finance.schemas import (
    FinanceOverviewRead,
    FinanceOverviewReceivableItem,
    FinanceOverviewSnapshotItem,
    FinanceOverviewSummaryRead,
    PaymentCreate,
    PaymentRecordRead,
    PaymentVerifyResponse,
    ReceivableRead,
)
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderRead
from erp_core.shared.enums import OrderMode, PaymentStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

DEPOSIT_MILESTONE_LABEL = "b2b_30_deposit"
BALANCE_MILESTONE_LABEL = "b2b_70_balance"
DEFAULT_DEPOSIT_RATIO = Decimal("0.30")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PaymentService:
    """B2B/B2C receivables, T/T bank slip L0 verification, and milestone order unlocking."""

    def __init__(
        self,
        order_service: OrderService | None = None,
        profit_service: OrderProfitService | None = None,
    ) -> None:
        self.orders = order_service or OrderService()
        self.profits = profit_service or OrderProfitService()

    def split_milestones(
        self, total_amount: Decimal, deposit_ratio: Decimal = DEFAULT_DEPOSIT_RATIO
    ) -> list[tuple[str, Decimal]]:
        """Split a total into deposit/balance milestones that always sum back exactly."""
        total = _money(total_amount)
        ratio = Decimal(deposit_ratio)
        if ratio <= Decimal("0") or ratio >= Decimal("1"):
            raise AppError(
                slug="invalid-deposit-ratio",
                status_code=422,
                code="ERR_INVALID_DEPOSIT_RATIO",
                title="Invalid Deposit Ratio",
                detail="deposit_ratio must be strictly between 0 and 1.",
            )
        deposit = _money(total * ratio)
        balance = _money(total - deposit)
        return [(DEPOSIT_MILESTONE_LABEL, deposit), (BALANCE_MILESTONE_LABEL, balance)]

    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal = DEFAULT_DEPOSIT_RATIO,
        due_date_deposit: date | None = None,
        due_date_balance: date | None = None,
    ) -> list[ReceivableRead]:
        """Persist the receivable milestones for a B2B order (idempotent per order+label)."""
        await self._lock_order(session, tenant_id, order_id)
        existing = (
            (
                await session.execute(
                    select(ReceivablePayable).where(
                        ReceivablePayable.tenant_id == tenant_id,
                        ReceivablePayable.order_id == order_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            return [ReceivableRead.model_validate(row) for row in existing]

        rows: list[ReceivablePayable] = []
        for label, amount in self.split_milestones(total_amount, deposit_ratio):
            receivable = ReceivablePayable(
                tenant_id=tenant_id,
                order_id=order_id,
                direction="receivable",
                milestone_label=label,
                due_amount=amount,
                paid_amount=Decimal("0.00"),
                currency=currency.upper(),
                status="open" if amount else "settled",
                due_date=due_date_deposit if label == DEPOSIT_MILESTONE_LABEL else due_date_balance,
            )
            session.add(receivable)
            rows.append(receivable)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="finance.payment_service",
            action="finance.receivables.created",
            resource_type="order",
            resource_id=order_id,
            after_state={
                "milestones": {r.milestone_label: str(r.due_amount) for r in rows},
                "currency": currency.upper(),
            },
        )
        return [ReceivableRead.model_validate(row) for row in rows]

    async def record_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        rp_id: UUID,
        amount_received: Decimal,
        currency: str,
        actual_fx_rate: Decimal,
        payment_method: str = "bank_transfer",
        gateway_fee: Decimal = Decimal("0.00"),
        external_tx_ref: str | None = None,
        verified_by_user_id: UUID | None = None,
    ) -> PaymentRecord:
        """Record a verified payment against a receivable and settle it when fully paid."""
        receivable = (
            await session.execute(
                select(ReceivablePayable).where(
                    ReceivablePayable.tenant_id == tenant_id,
                    ReceivablePayable.id == rp_id,
                )
            )
        ).scalar_one_or_none()
        if receivable is None:
            raise AppError(
                slug="receivable-not-found",
                status_code=404,
                code="ERR_RECEIVABLE_NOT_FOUND",
                title="Receivable Not Found",
                detail=f"Receivable '{rp_id}' does not exist.",
            )

        record = PaymentRecord(
            tenant_id=tenant_id,
            rp_id=rp_id,
            payment_method=payment_method,
            external_tx_ref=external_tx_ref,
            amount_received=_money(amount_received),
            currency=currency.upper(),
            actual_fx_rate=Decimal(actual_fx_rate),
            gateway_fee=_money(gateway_fee),
            verified_by_user_id=verified_by_user_id,
            paid_at=func.now(),
        )
        session.add(record)

        paid_total = _money(Decimal(receivable.paid_amount) + _money(amount_received))
        receivable.paid_amount = paid_total
        due = _money(Decimal(receivable.due_amount))
        if paid_total >= due:
            receivable.status = "settled"
        elif paid_total > Decimal("0.00"):
            receivable.status = "partial"
        await session.flush()

        await self.sync_order_payment_status(session, tenant_id, receivable.order_id)
        return record

    async def sync_order_payment_status(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> PaymentStatus:
        """Derive the order payment status from its receivable milestones.

        `orders` belongs to the orders module, so it is reached through a parameterized,
        RLS-scoped statement rather than a cross-module ORM model import.
        """
        rows = (
            (
                await session.execute(
                    select(ReceivablePayable).where(
                        ReceivablePayable.tenant_id == tenant_id,
                        ReceivablePayable.order_id == order_id,
                        ReceivablePayable.direction == "receivable",
                    )
                )
            )
            .scalars()
            .all()
        )
        order_exists = await session.scalar(
            text("SELECT 1 FROM orders WHERE tenant_id = :tid AND id = :oid"),
            {"tid": tenant_id, "oid": order_id},
        )
        if order_exists is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' does not exist.",
            )

        if not rows:
            status = PaymentStatus.UNPAID
        elif all(Decimal(r.paid_amount) >= Decimal(r.due_amount) for r in rows):
            status = PaymentStatus.PAID
        elif any(
            r.milestone_label == DEPOSIT_MILESTONE_LABEL and r.paid_amount >= r.due_amount
            for r in rows
        ):
            status = PaymentStatus.DEPOSIT_PAID
        elif any(r.paid_amount > 0 for r in rows):
            status = PaymentStatus.PARTIALLY_PAID
        else:
            status = PaymentStatus.UNPAID

        await session.execute(
            text(
                "UPDATE orders SET payment_status = :status, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :oid"
            ),
            {"status": status.value, "tid": tenant_id, "oid": order_id},
        )
        return status

    async def create_single_receivable(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        milestone_label: str,
        due_amount: Decimal,
        currency: str,
        status: str = "open",
        paid_amount: Decimal = Decimal("0.00"),
    ) -> ReceivableRead:
        rp = ReceivablePayable(
            tenant_id=tenant_id,
            order_id=order_id,
            direction="receivable",
            milestone_label=milestone_label,
            due_amount=due_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            paid_amount=paid_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            currency=currency.upper(),
            status=status,
        )
        session.add(rp)
        await session.flush()
        return ReceivableRead.model_validate(rp)

    async def list_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[ReceivableRead]:
        stmt = select(ReceivablePayable).where(ReceivablePayable.tenant_id == tenant_id)
        cnt = (
            select(func.count())
            .select_from(ReceivablePayable)
            .where(ReceivablePayable.tenant_id == tenant_id)
        )
        if order_id is not None:
            stmt = stmt.where(ReceivablePayable.order_id == order_id)
            cnt = cnt.where(ReceivablePayable.order_id == order_id)
        if status_filter is not None:
            stmt = stmt.where(ReceivablePayable.status == status_filter)
            cnt = cnt.where(ReceivablePayable.status == status_filter)

        total = int((await session.execute(cnt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(ReceivablePayable.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [ReceivableRead.model_validate(r) for r in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def _apply_verified_settlement(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        rp: ReceivablePayable,
        pr: PaymentRecord,
    ) -> OrderRead:
        amt = Decimal(str(pr.amount_received)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        new_paid = (Decimal(str(rp.paid_amount)) + amt).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        rp.paid_amount = new_paid
        rp.status = "settled" if new_paid >= Decimal(str(rp.due_amount)) else "partial"
        await session.flush()

        order_rps = (
            (
                await session.execute(
                    select(ReceivablePayable)
                    .where(
                        ReceivablePayable.tenant_id == tenant_id,
                        ReceivablePayable.order_id == rp.order_id,
                        ReceivablePayable.direction == "receivable",
                        ReceivablePayable.status != "void",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

        all_settled = bool(order_rps) and all(item.status == "settled" for item in order_rps)
        deposit_settled = any(
            item.milestone_label == "b2b_30_deposit" and item.status == "settled"
            for item in order_rps
        )
        if all_settled:
            order_read = await self.orders.apply_payment_milestone_state(
                session, tenant_id, rp.order_id, PaymentStatus.PAID, unlock_production=True
            )
        elif deposit_settled:
            order_read = await self.orders.apply_payment_milestone_state(
                session, tenant_id, rp.order_id, PaymentStatus.DEPOSIT_PAID, unlock_production=True
            )
        else:
            order_read = await self.orders.apply_payment_milestone_state(
                session,
                tenant_id,
                rp.order_id,
                PaymentStatus.PARTIALLY_PAID,
                unlock_production=False,
            )

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="payment.verified",
            resource_type="payment_record",
            resource_id=pr.id,
            after_state={
                "rp_id": str(rp.id),
                "order_id": str(rp.order_id),
                "amount_received": str(pr.amount_received),
                "actual_fx_rate": str(pr.actual_fx_rate),
                "rp_status": rp.status,
            },
        )
        await self.profits.recalculate_order_profit(session, tenant_id, rp.order_id)
        return order_read

    @staticmethod
    def _ensure_verifier_role(principal: CurrentPrincipal, tenant_id: UUID) -> None:
        if principal.tenant_id != tenant_id or principal.role not in {
            UserRole.OWNER,
            UserRole.FINANCE,
        }:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Payment Verification Forbidden",
                detail="Only Owner or Finance may verify bank slip payments.",
            )

    @staticmethod
    async def _lock_order(session: AsyncSession, tenant_id: UUID, order_id: UUID) -> None:
        order = await session.scalar(
            text("SELECT id FROM orders WHERE tenant_id = :tid AND id = :oid FOR UPDATE"),
            {"tid": tenant_id, "oid": order_id},
        )
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail="The order was not found.",
            )

    async def _lock_open_receivable(
        self, session: AsyncSession, tenant_id: UUID, rp_id: UUID
    ) -> ReceivablePayable:
        # Parent first: all settlements for one order share the same lock order.
        oid = await session.scalar(
            select(ReceivablePayable.order_id).where(
                ReceivablePayable.tenant_id == tenant_id,
                ReceivablePayable.id == rp_id,
            )
        )
        if oid is not None:
            await self._lock_order(session, tenant_id, oid)
        rp = (
            await session.execute(
                select(ReceivablePayable)
                .where(ReceivablePayable.tenant_id == tenant_id, ReceivablePayable.id == rp_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if rp is None:
            raise AppError(
                slug="receivable-not-found",
                status_code=404,
                code="ERR_RECEIVABLE_NOT_FOUND",
                title="Receivable Not Found",
                detail="Receivable not found.",
            )
        if rp.status in ("settled", "void") or rp.direction != "receivable":
            raise AppError(
                slug="receivable-already-settled",
                status_code=409,
                code="ERR_RECEIVABLE_ALREADY_SETTLED",
                title="Receivable Settled or Void",
                detail=f"Receivable '{rp.id}' is already in status '{rp.status}'.",
            )
        return rp

    @staticmethod
    def _check_payment_currency(currency: str, expected: str) -> None:
        if currency.upper() != expected.upper():
            raise AppError(
                slug="payment-currency-mismatch",
                status_code=422,
                code="ERR_PAYMENT_CURRENCY_MISMATCH",
                title="Payment Currency Mismatch",
                detail="Payment must be denominated in the receivable's currency.",
            )

    async def record_and_verify_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: PaymentCreate,
    ) -> PaymentRecordRead:
        self._ensure_verifier_role(principal, tenant_id)
        rp = await self._lock_open_receivable(session, tenant_id, payload.rp_id)
        self._check_payment_currency(payload.currency, rp.currency)

        amt = payload.amount_received.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pr = PaymentRecord(
            tenant_id=tenant_id,
            rp_id=rp.id,
            payment_method=payload.payment_method,
            external_tx_ref=payload.external_tx_ref,
            amount_received=amt,
            currency=payload.currency.upper(),
            actual_fx_rate=payload.actual_fx_rate.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            gateway_fee=payload.gateway_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            receipt_r2_key=payload.receipt_r2_key,
            verified_by_user_id=principal.user_id,
            paid_at=payload.paid_at or datetime.now(UTC),
        )
        session.add(pr)
        await session.flush()
        await self._apply_verified_settlement(session, tenant_id, principal, rp, pr)
        return PaymentRecordRead.model_validate(pr)

    async def submit_payment_receipt(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: PaymentCreate,
    ) -> PaymentRecordRead:
        if principal.tenant_id != tenant_id:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Payment Forbidden",
                detail="Payment tenant does not match the user.",
            )
        rp = await self._lock_open_receivable(session, tenant_id, payload.rp_id)
        self._check_payment_currency(payload.currency, rp.currency)
        pr = PaymentRecord(
            tenant_id=tenant_id,
            rp_id=rp.id,
            payment_method=payload.payment_method,
            external_tx_ref=payload.external_tx_ref,
            amount_received=payload.amount_received.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            currency=payload.currency.upper(),
            actual_fx_rate=payload.actual_fx_rate.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            gateway_fee=payload.gateway_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            receipt_r2_key=payload.receipt_r2_key,
            verified_by_user_id=None,
            paid_at=payload.paid_at or datetime.now(UTC),
        )
        session.add(pr)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="payment.submitted",
            resource_type="payment_record",
            resource_id=pr.id,
            after_state={"rp_id": str(rp.id), "external_tx_ref": pr.external_tx_ref},
        )
        return PaymentRecordRead.model_validate(pr)

    async def verify_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payment_id: UUID,
    ) -> PaymentVerifyResponse:
        self._ensure_verifier_role(principal, tenant_id)
        pr = (
            await session.execute(
                select(PaymentRecord)
                .where(PaymentRecord.tenant_id == tenant_id, PaymentRecord.id == payment_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if pr is None:
            raise AppError(
                slug="payment-not-found",
                status_code=404,
                code="ERR_PAYMENT_NOT_FOUND",
                title="Payment Record Not Found",
                detail=f"Payment record '{payment_id}' not found.",
            )
        if pr.verified_by_user_id is not None:
            raise AppError(
                slug="payment-already-verified",
                status_code=409,
                code="ERR_PAYMENT_ALREADY_VERIFIED",
                title="Payment Already Verified",
                detail=f"Payment record '{payment_id}' has already been verified.",
            )
        rp = await self._lock_open_receivable(session, tenant_id, pr.rp_id)
        self._check_payment_currency(pr.currency, rp.currency)
        pr.verified_by_user_id = principal.user_id
        order_read = await self._apply_verified_settlement(session, tenant_id, principal, rp, pr)
        return PaymentVerifyResponse(
            ok=True,
            payment=PaymentRecordRead.model_validate(pr),
            order_fulfillment_status=order_read.fulfillment_status,
        )

    async def get_overview(self, session: AsyncSession, tenant_id: UUID) -> FinanceOverviewRead:
        summary = await self.profits.get_profit_summary(session, tenant_id)
        rp_rows = (
            await session.execute(
                text(
                    """
                    SELECT rp.id, rp.order_id, o.order_no, o.order_mode, rp.milestone_label,
                           rp.due_amount, rp.paid_amount, rp.currency, rp.status,
                           CASE
                               WHEN o.order_mode IN ('b2b_wholesale', 'b2b_sample')
                                    AND EXISTS (
                                        SELECT 1 FROM receivables_payables r70
                                        WHERE r70.tenant_id = rp.tenant_id
                                          AND r70.order_id = rp.order_id
                                          AND r70.milestone_label = 'b2b_70_balance'
                                          AND r70.status <> 'settled'
                                    )
                               THEN true
                               ELSE false
                           END AS shipment_blocked_70_unpaid,
                           pend.id AS pending_payment_id,
                           pend.external_tx_ref AS pending_tx_ref
                    FROM receivables_payables rp
                    JOIN orders o ON o.id = rp.order_id AND o.tenant_id = rp.tenant_id
                    LEFT JOIN LATERAL (
                        SELECT pr.id, pr.external_tx_ref
                        FROM payment_records pr
                        WHERE pr.tenant_id = rp.tenant_id
                          AND pr.rp_id = rp.id
                          AND pr.verified_by_user_id IS NULL
                        ORDER BY pr.created_at DESC
                        LIMIT 1
                    ) pend ON true
                    WHERE rp.tenant_id = :tid AND rp.direction = 'receivable'
                    ORDER BY rp.created_at DESC
                    LIMIT 100
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()

        snap_rows = (
            await session.execute(
                text(
                    """
                    SELECT s.order_id, o.order_no, s.base_currency, s.gross_revenue_base,
                           s.cogs_base, s.platform_commission_base, s.payment_gateway_fee_base,
                           s.shipping_and_duty_base, s.channel_msg_cost_base,
                           s.ad_attribution_cost_base, s.fx_gain_loss_base,
                           s.net_contribution_margin_base
                    FROM order_profit_snapshots s
                    JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                    WHERE s.tenant_id = :tid
                    ORDER BY s.recalculated_at DESC
                    LIMIT 100
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()

        return FinanceOverviewRead(
            summary=FinanceOverviewSummaryRead(
                total_gross_revenue_base=summary.gross_revenue_base,
                total_net_margin_base=summary.net_contribution_margin_base,
                avg_margin_pct=summary.contribution_margin_pct,
                base_currency=summary.base_currency,
            ),
            receivables=[
                FinanceOverviewReceivableItem(
                    id=r.id,
                    order_id=r.order_id,
                    order_no=str(r.order_no),
                    order_mode=OrderMode(str(r.order_mode)),
                    milestone_label=str(r.milestone_label),
                    due_amount=Decimal(str(r.due_amount)),
                    paid_amount=Decimal(str(r.paid_amount)),
                    currency=str(r.currency),
                    status=str(r.status),
                    shipment_blocked_70_unpaid=bool(r.shipment_blocked_70_unpaid),
                    pending_payment_id=r.pending_payment_id,
                    pending_tx_ref=r.pending_tx_ref,
                )
                for r in rp_rows
            ],
            cvp_snapshots=[
                FinanceOverviewSnapshotItem(
                    order_id=s.order_id,
                    order_no=str(s.order_no),
                    base_currency=str(s.base_currency),
                    gross_revenue_base=Decimal(str(s.gross_revenue_base)),
                    cogs_base=Decimal(str(s.cogs_base)),
                    platform_commission_base=Decimal(str(s.platform_commission_base)),
                    payment_gateway_fee_base=Decimal(str(s.payment_gateway_fee_base)),
                    shipping_and_duty_base=Decimal(str(s.shipping_and_duty_base)),
                    channel_msg_cost_base=Decimal(str(s.channel_msg_cost_base)),
                    ad_attribution_cost_base=Decimal(str(s.ad_attribution_cost_base)),
                    fx_gain_loss_base=Decimal(str(s.fx_gain_loss_base)),
                    net_contribution_margin_base=Decimal(str(s.net_contribution_margin_base)),
                )
                for s in snap_rows
            ],
        )

    async def export_cvp_csv(self, session: AsyncSession, tenant_id: UUID) -> str:
        overview = await self.get_overview(session, tenant_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "order_id",
                "order_no",
                "base_currency",
                "gross_revenue_base",
                "cogs_base",
                "platform_commission_base",
                "payment_gateway_fee_base",
                "shipping_and_duty_base",
                "channel_msg_cost_base",
                "ad_attribution_cost_base",
                "fx_gain_loss_base",
                "net_contribution_margin_base",
            ]
        )
        for snap in overview.cvp_snapshots:
            writer.writerow(
                [
                    str(snap.order_id),
                    snap.order_no,
                    snap.base_currency,
                    str(snap.gross_revenue_base),
                    str(snap.cogs_base),
                    str(snap.platform_commission_base),
                    str(snap.payment_gateway_fee_base),
                    str(snap.shipping_and_duty_base),
                    str(snap.channel_msg_cost_base),
                    str(snap.ad_attribution_cost_base),
                    str(snap.fx_gain_loss_base),
                    str(snap.net_contribution_margin_base),
                ]
            )
        return buf.getvalue()
