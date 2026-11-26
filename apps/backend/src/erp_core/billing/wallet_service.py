from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.billing.models import CreditWallet, UsageLedger
from erp_core.billing.schemas import CreditWalletRead
from erp_core.shared.enums import AutonomyLevel, UsageMetricType
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

PLAN_INCLUDED_CREDITS = {"starter": 2000, "growth": 8000, "scale": 24000, "enterprise": 100000}
OVERAGE_CENTS_PER_CREDIT = Decimal("1")
CREDIT_QUANTUM = Decimal("0.0001")
_COST_QUANTUM = Decimal("0.000001")
FUNDING_KEY_PREFIX = "funding:stripe:"
#: Ledger-note prefix for non-monetary billing events (downgrades / payment failures).
BILLING_EVENT_KEY_PREFIX = "billing:event:"
DOWNGRADE_PLAN_TIER = "starter"
DOWNGRADE_REASONS = frozenset({"invoice_payment_failed", "subscription_deleted"})
_BILLING_REFERENCE = re.compile(r"[A-Za-z0-9_]{1,200}")
_ALLOWED: tuple[bool, AutonomyLevel | None] = True, None
_DENIED: tuple[bool, AutonomyLevel | None] = False, AutonomyLevel.L0_COPILOT


def _error(status: int, code: str, detail: str) -> AppError:
    title = code.removeprefix("ERR_").replace("_", " ")
    return AppError(
        slug=title.lower().replace(" ", "-"),
        title=title.title(),
        status_code=status,
        code=code,
        detail=detail,
    )


def _wheel(wallet: CreditWallet) -> CreditWalletRead:
    included = _included(wallet)
    return CreditWalletRead(
        tenant_id=wallet.tenant_id,
        plan_tier=wallet.plan_tier,
        included_credits_remaining=included,
        topup_credits_balance=wallet.topup_credits_balance,
        total_credits_available=included + wallet.topup_credits_balance,
        fractional_credit_carry=wallet.fractional_credit_carry,
        overage_spend_cap_cents=wallet.overage_spend_cap_cents,
        overage_spent_cents=wallet.overage_spent_cents,
        auto_downgrade_to_l0=wallet.auto_downgrade_to_l0,
        billing_cycle_end=wallet.billing_cycle_end,
        updated_at=wallet.updated_at,
    )


def _included(wallet: CreditWallet) -> int:
    return wallet.included_credits_remaining if wallet.billing_cycle_end > datetime.now(UTC) else 0


def _within_budget(wallet: CreditWallet, units: Decimal) -> bool:
    balance = Decimal(_included(wallet) + wallet.topup_credits_balance)
    if not wallet.auto_downgrade_to_l0 and wallet.billing_cycle_end > datetime.now(UTC):
        balance += (
            Decimal(max(0, wallet.overage_spend_cap_cents - wallet.overage_spent_cents))
            / OVERAGE_CENTS_PER_CREDIT
        )
    return wallet.fractional_credit_carry + units <= balance


def _exact(value: Decimal, quantum: Decimal, maximum: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal(0) <= value < maximum:
        raise _error(
            422, "ERR_INVALID_USAGE_AMOUNT", "Expected a nonnegative finite Decimal in range."
        )
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded >= maximum:
        raise _error(422, "ERR_INVALID_USAGE_AMOUNT", "Rounded amount exceeds the ledger range.")
    return rounded


class CreditWalletService:
    """A tenant wallet row serializes funding and consumption of that tenant's credits."""

    @staticmethod
    async def _wallet(
        session: AsyncSession, tenant_id: UUID, lock: bool = False
    ) -> CreditWallet | None:
        query = select(CreditWallet).where(CreditWallet.tenant_id == tenant_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await session.scalar(query)

    async def ensure_wallet(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        plan_tier: str = "starter",
        included_credits: int | None = None,
        topup_credits: int = 0,
        billing_cycle_end: datetime | None = None,
    ) -> CreditWalletRead:
        wallet = await self._wallet(session, tenant_id)
        if wallet is None:
            stmt = insert(CreditWallet).values(
                tenant_id=tenant_id,
                plan_tier=plan_tier,
                included_credits_remaining=PLAN_INCLUDED_CREDITS[plan_tier]
                if included_credits is None
                else included_credits,
                topup_credits_balance=topup_credits,
                billing_cycle_end=billing_cycle_end or datetime.now(UTC) + timedelta(days=30),
            )
            await session.execute(
                stmt.on_conflict_do_nothing(index_elements=[CreditWallet.tenant_id])
            )
        return await self.get_wallet(session, tenant_id)

    async def get_wallet(self, session: AsyncSession, tenant_id: UUID) -> CreditWalletRead:
        wallet = await self._wallet(session, tenant_id)
        if wallet is None:
            raise _error(404, "ERR_WALLET_NOT_FOUND", "Wallet was not found for this tenant.")
        return _wheel(wallet)

    async def check_ai_execution_allowed(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        estimated_credits: int = 1,
    ) -> tuple[bool, AutonomyLevel | None]:
        wallet = await self._wallet(session, tenant_id)
        return (
            _ALLOWED
            if wallet and _within_budget(wallet, Decimal(max(1, estimated_credits)))
            else _DENIED
        )

    async def lock_budget(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        estimated_credits: Decimal,
    ) -> bool:
        # The caller holds this capacity until it records actual usage and commits or rolls back.
        wallet = await self._wallet(session, tenant_id, lock=True)
        return wallet is not None and _within_budget(wallet, estimated_credits)

    async def record_usage(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        idempotency_key: str,
        metric_type: UsageMetricType,
        units: Decimal,
        raw_cost_usd: Decimal,
        provider: str,
    ) -> None:
        allowed, _ = await self.try_reserve_and_record_spend(
            session,
            tenant_id,
            idempotency_key,
            metric_type,
            units,
            raw_cost_usd,
            provider,
        )
        if not allowed:
            raise _error(402, "ERR_CREDITS_DEPLETED", "Credit balance cannot cover this usage.")

    async def try_reserve_and_record_spend(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        idempotency_key: str,
        metric_type: UsageMetricType,
        units: Decimal,
        raw_cost_usd: Decimal,
        provider: str,
    ) -> tuple[bool, AutonomyLevel | None]:
        credits = _exact(units, CREDIT_QUANTUM, Decimal("1e8"))
        cost = _exact(raw_cost_usd, _COST_QUANTUM, Decimal("1e4"))
        if (
            not idempotency_key
            or len(idempotency_key) > 255
            or idempotency_key.startswith(FUNDING_KEY_PREFIX)
        ):
            raise _error(422, "ERR_INVALID_USAGE_KEY", "Invalid usage idempotency key.")
        wallet = await self._wallet(session, tenant_id, lock=True)
        if wallet is None:
            return _DENIED
        previous = await session.scalar(
            select(UsageLedger).where(
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.idempotency_key == idempotency_key,
            )
        )
        if previous is not None:
            if (
                previous.metric_type,
                previous.units_consumed,
                previous.raw_provider_cost_usd,
                previous.model_or_provider,
            ) != (metric_type.value, credits, cost, provider):
                raise _error(
                    409, "ERR_USAGE_KEY_CONFLICT", "Usage key has different accounting data."
                )
            return _ALLOWED
        if not _within_budget(wallet, credits):
            return _DENIED
        await session.execute(
            insert(UsageLedger).values(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                metric_type=metric_type.value,
                units_consumed=credits,
                raw_provider_cost_usd=cost,
                model_or_provider=provider,
            )
        )
        accrued = wallet.fractional_credit_carry + credits
        whole = int(accrued.to_integral_value(rounding=ROUND_FLOOR))
        inc = min(_included(wallet), whole)
        topup = min(wallet.topup_credits_balance, whole - inc)
        overage = int(Decimal(whole - inc - topup) * OVERAGE_CENTS_PER_CREDIT)
        await session.execute(
            update(CreditWallet)
            .where(CreditWallet.tenant_id == tenant_id)
            .values(
                included_credits_remaining=_included(wallet) - inc,
                topup_credits_balance=wallet.topup_credits_balance - topup,
                overage_spent_cents=wallet.overage_spent_cents + overage,
                fractional_credit_carry=accrued - Decimal(whole),
                updated_at=datetime.now(UTC),
            )
        )
        return _ALLOWED

    async def apply_stripe_topup(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payment_id: str,
        credits: int,
    ) -> bool:
        if not 0 < credits <= 1_000_000:
            raise _error(422, "ERR_INVALID_TOPUP", "Invalid credit purchase amount.")
        await self.ensure_wallet(session, tenant_id=tenant_id)
        stmt = insert(UsageLedger).values(
            tenant_id=tenant_id,
            idempotency_key=f"{FUNDING_KEY_PREFIX}{payment_id}",
            metric_type=UsageMetricType.AI_CREDIT.value,
            units_consumed=Decimal(credits),
            raw_provider_cost_usd=Decimal(0),
            model_or_provider="stripe_topup",
            stripe_meter_synced=True,
        )
        funded = await session.scalar(
            stmt.on_conflict_do_nothing(
                index_elements=[UsageLedger.tenant_id, UsageLedger.idempotency_key],
            ).returning(UsageLedger.id)
        )
        if funded is None:
            return False
        await session.execute(
            update(CreditWallet)
            .where(CreditWallet.tenant_id == tenant_id)
            .values(
                topup_credits_balance=CreditWallet.topup_credits_balance + credits,
                updated_at=datetime.now(UTC),
            )
        )
        await record_audit_event(
            session,
            tenant_id,
            "webhook",
            payment_id,
            "billing.wallet.topup",
            "credit_wallet",
            tenant_id,
            after_state={"credits_added": credits},
        )
        return True

    async def apply_billing_downgrade(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        reference: str,
        *,
        reason: str,
    ) -> bool:
        """Fail the tenant down to `starter` once per Stripe object.

        Called for `invoice.payment_failed` / `customer.subscription.deleted`.
        The `usage_ledger` marker row (`billing:event:<stripe id>`) is the
        idempotency gate, so a Stripe replay storm can never double-apply the
        downgrade or re-open overage spending.
        """
        if not isinstance(reference, str) or not _BILLING_REFERENCE.fullmatch(reference):
            raise _error(
                422, "ERR_INVALID_BILLING_REFERENCE", "Invalid Stripe billing object ID."
            )
        if reason not in DOWNGRADE_REASONS:
            raise _error(422, "ERR_INVALID_BILLING_REASON", "Unknown billing downgrade reason.")
        await self.ensure_wallet(session, tenant_id=tenant_id)
        stmt = insert(UsageLedger).values(
            tenant_id=tenant_id,
            idempotency_key=f"{BILLING_EVENT_KEY_PREFIX}{reference}",
            metric_type=UsageMetricType.AI_CREDIT.value,
            units_consumed=Decimal(0),
            raw_provider_cost_usd=Decimal(0),
            model_or_provider="stripe_downgrade",
            stripe_meter_synced=True,
        )
        marker = await session.scalar(
            stmt.on_conflict_do_nothing(
                index_elements=[UsageLedger.tenant_id, UsageLedger.idempotency_key],
            ).returning(UsageLedger.id)
        )
        if marker is None:
            return False
        await session.execute(
            update(CreditWallet)
            .where(CreditWallet.tenant_id == tenant_id)
            .values(
                plan_tier=DOWNGRADE_PLAN_TIER,
                included_credits_remaining=func.least(
                    CreditWallet.included_credits_remaining,
                    PLAN_INCLUDED_CREDITS[DOWNGRADE_PLAN_TIER],
                ),
                overage_spend_cap_cents=0,
                auto_downgrade_to_l0=True,
                updated_at=datetime.now(UTC),
            )
        )
        await record_audit_event(
            session,
            tenant_id,
            "webhook",
            reference,
            "billing.plan.downgraded",
            "credit_wallet",
            tenant_id,
            after_state={
                "plan_tier": DOWNGRADE_PLAN_TIER,
                "overage_spend_cap_cents": 0,
                "auto_downgrade_to_l0": True,
                "reason": reason,
            },
        )
        return True
