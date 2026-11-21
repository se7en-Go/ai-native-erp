"""Supplier accounts-payable (AP) ledger that closes the Finance AR <-> AP loop.

`payables` (and `sourcing_drafts`) are created by the locked Alembic revision
`0012_ai_autoreply_sourcing_ap`; `payables` deliberately has **no** ORM model in
`erp_core.finance.models`, and `purchase_orders` / `suppliers` belong to the
`supply_chain` module. Every statement here is therefore explicit, parameterised
SQL scoped by `tenant_id`, exactly like `PaymentService` reaches `orders`: the
`RESTRICTIVE` RLS policy is the second line of defence, never the only one.

Financial invariants enforced in this module:

* money is `decimal.Decimal` quantised to 2 dp with `ROUND_HALF_UP` - a `float`
  never reaches the ledger (`ExactDecimal` rejects it at the API boundary);
* a payment can never exceed the outstanding balance (no accidental overpay),
  and the status machine is strictly ``open -> partial -> paid``;
* the purchase-order -> payable sync is idempotent per ``(tenant_id, po_id)``
  and serialised per tenant with a transaction-scoped advisory lock, so two
  concurrent ``GET /payables`` calls cannot double-book the same PO.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.schemas import ExactDecimal
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

#: Roles allowed to read the AP ledger and the cash-flow summary (L0 read).
AP_READ_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.FINANCE,
)
#: Roles allowed to核销 supplier money out (L0 fund-red-line, deliberately narrower).
AP_PAY_ROLES: tuple[UserRole, ...] = (UserRole.OWNER, UserRole.FINANCE)

_PAYABLE_COLUMNS = (
    "id, tenant_id, po_id, supplier_id, po_no, supplier_name, due_amount, "
    "paid_amount, currency, due_date, status, payment_ref, paid_at, created_at, updated_at"
)

# POs that were cancelled never become a liability; everything else (draft,
# ordered, partially_received, received) is real committed spend and is booked.
_SYNC_PAYABLES_SQL = """
    INSERT INTO payables (
        tenant_id, po_id, supplier_id, po_no, supplier_name,
        due_amount, paid_amount, currency, due_date, status
    )
    SELECT
        po.tenant_id,
        po.id,
        po.supplier_id,
        po.po_no,
        COALESCE(s.name, ''),
        po.total_amount,
        0.00,
        UPPER(po.currency),
        po.expected_arrival_date,
        'open'
    FROM purchase_orders po
    LEFT JOIN suppliers s
        ON s.tenant_id = po.tenant_id AND s.id = po.supplier_id
    WHERE po.tenant_id = :tid
      AND po.status <> 'cancelled'
      AND NOT EXISTS (
          SELECT 1
          FROM payables p
          WHERE p.tenant_id = po.tenant_id AND p.po_id = po.id
      )
    RETURNING id, po_no, supplier_name, due_amount, currency
"""

_LIST_PAYABLES_SQL = f"""
    SELECT {_PAYABLE_COLUMNS}
    FROM payables
    WHERE tenant_id = :tid
    ORDER BY created_at DESC, id
"""

_PAYABLES_TOTALS_SQL = """
    SELECT
        COALESCE(SUM(due_amount), 0)               AS total_due,
        COALESCE(SUM(paid_amount), 0)              AS total_paid,
        COALESCE(SUM(due_amount - paid_amount), 0) AS total_outstanding
    FROM payables
    WHERE tenant_id = :tid
      AND UPPER(currency) = UPPER(:ccy)
"""

_AR_TOTALS_SQL = """
    SELECT
        COALESCE(SUM(due_amount), 0)  AS total_due,
        COALESCE(SUM(paid_amount), 0) AS total_paid
    FROM receivables_payables
    WHERE tenant_id = :tid
      AND direction = 'receivable'
      AND status <> 'void'
      AND UPPER(currency) = UPPER(:ccy)
"""

_AP_TOTALS_SQL = """
    SELECT
        COALESCE(SUM(due_amount), 0)  AS total_due,
        COALESCE(SUM(paid_amount), 0) AS total_paid
    FROM payables
    WHERE tenant_id = :tid
      AND UPPER(currency) = UPPER(:ccy)
"""


def _money(value: Decimal | int | str) -> Decimal:
    """Quantise any monetary input to the ledger's 2-dp `NUMERIC(14, 2)` scale."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PayableRead(BaseModel):
    """One supplier AP document as stored in `public.payables`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    po_id: UUID | None
    supplier_id: UUID | None
    po_no: str
    supplier_name: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    due_date: date | None
    status: str
    payment_ref: str | None
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PayableListRead(BaseModel):
    """The tenant AP ledger plus its base-currency totals."""

    items: list[PayableRead]
    total_due: Decimal
    total_paid: Decimal
    total_outstanding: Decimal
    currency: str


class PayablePaymentRequest(BaseModel):
    """A supplier settlement. `amount` must be an exact decimal, never a float."""

    amount: ExactDecimal = Field(gt=Decimal("0"), max_digits=14, decimal_places=2)
    payment_ref: str = Field(default="", max_length=128)
    paid_at: datetime | None = None

    @field_validator("payment_ref")
    @classmethod
    def _strip_ref(cls, value: str) -> str:
        return value.strip()


class CashflowSummaryRead(BaseModel):
    """AR/AP position and net cash flow, all denominated in the tenant base currency."""

    currency: str
    ar_total: Decimal
    ar_collected: Decimal
    ar_outstanding: Decimal
    ap_total: Decimal
    ap_paid: Decimal
    ap_outstanding: Decimal
    net_cashflow: Decimal


class PayableService:
    """Supplier AP ledger: PO ingestion, L0 settlement and the cash-flow summary."""

    async def sync_purchase_orders(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal | None = None,
    ) -> int:
        """Book an `open` payable for every purchase order that has none yet.

        Idempotent per ``(tenant_id, po_id)``: the ``NOT EXISTS`` guard makes a
        repeated call a no-op, and the advisory lock (released at transaction
        end) stops two racing requests from booking the same PO twice.
        """
        # Advisory lock key: one writer per tenant for the whole sync statement.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": f"finance.ap.sync:{tenant_id}"},
        )
        created = (
            (
                await session.execute(
                    text(_SYNC_PAYABLES_SQL),
                    {"tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
        actor_type: Literal["human_user", "system_cron"] = (
            "human_user" if principal is not None else "system_cron"
        )
        actor_id = str(principal.user_id) if principal is not None else "finance.ap_service"
        for row in created:
            await record_audit_event(
                session,
                tenant_id=tenant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action="finance.payable.created_from_purchase_order",
                resource_type="payable",
                resource_id=row["id"],
                after_state={
                    "po_no": str(row["po_no"]),
                    "supplier_name": str(row["supplier_name"]),
                    "due_amount": str(_money(row["due_amount"])),
                    "currency": str(row["currency"]),
                    "status": "open",
                },
            )
        return len(created)

    async def list_payables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal | None = None,
    ) -> PayableListRead:
        """Sync fresh POs, then return the whole ledger with base-currency totals.

        `items` lists every payable of the tenant in its own currency; the three
        totals are the SQL aggregate of the rows denominated in the tenant base
        currency, so no cross-currency figure is silently added together.
        """
        await self.sync_purchase_orders(session, tenant_id, principal)
        currency = await self._base_currency(session, tenant_id)
        rows = (
            (await session.execute(text(_LIST_PAYABLES_SQL), {"tid": tenant_id}))
            .mappings()
            .all()
        )
        totals = (
            (
                await session.execute(
                    text(_PAYABLES_TOTALS_SQL), {"tid": tenant_id, "ccy": currency}
                )
            )
            .mappings()
            .one()
        )
        total_due = _money(totals["total_due"])
        total_paid = _money(totals["total_paid"])
        return PayableListRead(
            items=[self._to_read(row) for row in rows],
            total_due=total_due,
            total_paid=total_paid,
            total_outstanding=_money(totals["total_outstanding"]),
            currency=currency,
        )

    async def record_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payable_id: UUID,
        payload: PayablePaymentRequest,
    ) -> PayableRead:
        """Settle a payable (L0 fund red line) and advance ``open -> partial -> paid``."""
        self._ensure_payer(principal, tenant_id)
        row = (
            await session.execute(
                text(
                    f"SELECT {_PAYABLE_COLUMNS} FROM payables "
                    "WHERE tenant_id = :tid AND id = :pid FOR UPDATE"
                ),
                {"tid": tenant_id, "pid": payable_id},
            )
        ).mappings().first()
        if row is None:
            raise AppError(
                slug="payable-not-found",
                status_code=404,
                code="ERR_PAYABLE_NOT_FOUND",
                title="Payable Not Found",
                detail=f"Payable '{payable_id}' does not exist.",
            )

        amount = _money(payload.amount)
        due = _money(row["due_amount"])
        already_paid = _money(row["paid_amount"])
        outstanding = _money(due - already_paid)
        if str(row["status"]) == "paid" or outstanding <= Decimal("0.00"):
            raise AppError(
                slug="payable-already-paid",
                status_code=409,
                code="ERR_PAYABLE_ALREADY_PAID",
                title="Payable Already Settled",
                detail=f"Payable '{payable_id}' is already fully paid.",
            )
        if amount > outstanding:
            raise AppError(
                slug="payable-overpayment",
                status_code=422,
                code="ERR_PAYABLE_OVERPAYMENT",
                title="Payable Overpayment Rejected",
                detail=(
                    f"Payment of {amount} exceeds the outstanding balance {outstanding} "
                    f"of payable '{payable_id}'."
                ),
            )

        new_paid = _money(already_paid + amount)
        new_status = "paid" if new_paid >= due else "partial"
        settled_at = payload.paid_at or datetime.now(UTC)
        payment_ref = payload.payment_ref or row["payment_ref"]

        updated = (
            await session.execute(
                text(
                    "UPDATE payables SET "
                    "paid_amount = :paid, status = :status, payment_ref = :ref, "
                    "paid_at = :paid_at, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :pid "
                    f"RETURNING {_PAYABLE_COLUMNS}"
                ),
                {
                    "paid": new_paid,
                    "status": new_status,
                    "ref": payment_ref,
                    "paid_at": settled_at,
                    "tid": tenant_id,
                    "pid": payable_id,
                },
            )
        ).mappings().one()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="finance.payable.paid",
            resource_type="payable",
            resource_id=payable_id,
            before_state={
                "paid_amount": str(already_paid),
                "outstanding": str(outstanding),
                "status": str(row["status"]),
            },
            after_state={
                "paid_amount": str(new_paid),
                "amount": str(amount),
                "outstanding": str(_money(due - new_paid)),
                "status": new_status,
                "payment_ref": payment_ref,
            },
        )
        return self._to_read(updated)

    async def cashflow_summary(
        self, session: AsyncSession, tenant_id: UUID
    ) -> CashflowSummaryRead:
        """AR vs AP position and net cash flow, straight from SQL aggregates."""
        currency = await self._base_currency(session, tenant_id)
        ar = (
            (
                await session.execute(
                    text(_AR_TOTALS_SQL), {"tid": tenant_id, "ccy": currency}
                )
            )
            .mappings()
            .one()
        )
        ap = (
            (
                await session.execute(
                    text(_AP_TOTALS_SQL), {"tid": tenant_id, "ccy": currency}
                )
            )
            .mappings()
            .one()
        )
        ar_total = _money(ar["total_due"])
        ar_collected = _money(ar["total_paid"])
        ap_total = _money(ap["total_due"])
        ap_paid = _money(ap["total_paid"])
        return CashflowSummaryRead(
            currency=currency,
            ar_total=ar_total,
            ar_collected=ar_collected,
            ar_outstanding=_money(ar_total - ar_collected),
            ap_total=ap_total,
            ap_paid=ap_paid,
            ap_outstanding=_money(ap_total - ap_paid),
            net_cashflow=_money(ar_collected - ap_paid),
        )

    @staticmethod
    def _to_read(row: Mapping[str, Any] | RowMapping) -> PayableRead:
        return PayableRead(
            id=row["id"],
            tenant_id=row["tenant_id"],
            po_id=row["po_id"],
            supplier_id=row["supplier_id"],
            po_no=str(row["po_no"]),
            supplier_name=str(row["supplier_name"]),
            due_amount=_money(row["due_amount"]),
            paid_amount=_money(row["paid_amount"]),
            currency=str(row["currency"]).upper(),
            due_date=row["due_date"],
            status=str(row["status"]),
            payment_ref=row["payment_ref"],
            paid_at=row["paid_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    async def _base_currency(session: AsyncSession, tenant_id: UUID) -> str:
        value = await session.scalar(
            text("SELECT base_currency FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )
        if value is None:
            raise AppError(
                slug="tenant-not-found",
                status_code=404,
                code="ERR_TENANT_NOT_FOUND",
                title="Tenant Not Found",
                detail=f"Tenant '{tenant_id}' does not exist.",
            )
        return str(value).upper()

    @staticmethod
    def _ensure_payer(principal: CurrentPrincipal, tenant_id: UUID) -> None:
        if principal.tenant_id != tenant_id or principal.role not in AP_PAY_ROLES:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Payable Settlement Forbidden",
                detail="Only Owner or Finance may settle supplier payables.",
            )
