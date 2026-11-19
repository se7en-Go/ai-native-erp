"""Receivables/payables milestones and payment records (30% deposit / 70% balance).

Plan 03 Task 3.6 owns the full finance surface (bank slip verification, payment matching and
the CVP console API). This module provides the milestone allocation the B2B quote
`1-click` conversion needs, with `Decimal`-only money arithmetic and fail-closed RLS.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import PaymentRecord, ReceivablePayable
from erp_core.shared.enums import PaymentStatus
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

DEPOSIT_MILESTONE_LABEL = "b2b_30_deposit"
BALANCE_MILESTONE_LABEL = "b2b_70_balance"
DEFAULT_DEPOSIT_RATIO = Decimal("0.30")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PaymentService:
    """Milestone receivables and payment reconciliation for dual-mode orders."""

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
    ) -> list[ReceivablePayable]:
        """Persist the receivable milestones for a B2B order (idempotent per order+label)."""
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
            return list(existing)

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
                status="open",
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
        return rows

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
        elif any(Decimal(r.paid_amount) > Decimal("0.00") for r in rows):
            status = PaymentStatus.DEPOSIT_PAID if len(rows) > 1 else PaymentStatus.PARTIALLY_PAID
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
