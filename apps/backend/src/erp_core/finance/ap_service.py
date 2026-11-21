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
from sqlalchemy.exc import IntegrityError
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

# POs that were cancelled never become a liability, and a `draft` PO is not yet a
# committed liability either: only `ordered`, `partially_received` and `received`
# POs are real committed spend and are booked as AP documents.
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
      AND po.status IN ('ordered', 'partially_received', 'received')
      AND NOT EXISTS (
          SELECT 1
          FROM payables p
          WHERE p.tenant_id = po.tenant_id AND p.po_id = po.id
      )
    RETURNING id, po_no, supplier_name, due_amount, currency
"""

# A PO that is still `draft` (or was cancelled after booking) must never keep an
# untouched AP liability. `payables` only allows open/partial/paid (0012 CHECK), so
# an unpaid orphan is deleted rather than flipped to a non-existent `void` status;
# anything that already received money stays put as an auditable record.
_PURGE_ORPHAN_PAYABLES_SQL = """
    DELETE FROM payables p
    USING purchase_orders po
    WHERE p.tenant_id = :tid
      AND po.tenant_id = p.tenant_id
      AND po.id = p.po_id
      AND po.status IN ('draft', 'cancelled')
      AND p.status = 'open'
      AND p.paid_amount = 0
    RETURNING p.id, p.po_no, po.status AS po_status
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

# Per-currency AR/AP position. A FULL OUTER JOIN keeps currencies that exist on only
# one side of the ledger, so a foreign-currency receivable can never be hidden.
_BY_CURRENCY_SQL = """
    WITH ar AS (
        SELECT
            UPPER(TRIM(currency))          AS ccy,
            COALESCE(SUM(due_amount), 0)   AS total_due,
            COALESCE(SUM(paid_amount), 0)  AS total_paid
        FROM receivables_payables
        WHERE tenant_id = :tid
          AND direction = 'receivable'
          AND status <> 'void'
        GROUP BY UPPER(TRIM(currency))
    ), ap AS (
        SELECT
            UPPER(TRIM(currency))          AS ccy,
            COALESCE(SUM(due_amount), 0)   AS total_due,
            COALESCE(SUM(paid_amount), 0)  AS total_paid
        FROM payables
        WHERE tenant_id = :tid
        GROUP BY UPPER(TRIM(currency))
    )
    SELECT
        COALESCE(ar.ccy, ap.ccy)     AS ccy,
        COALESCE(ar.total_due, 0)    AS ar_total,
        COALESCE(ar.total_paid, 0)   AS ar_collected,
        COALESCE(ap.total_due, 0)    AS ap_total,
        COALESCE(ap.total_paid, 0)   AS ap_paid
    FROM ar
    FULL OUTER JOIN ap ON ap.ccy = ar.ccy
    ORDER BY ccy
"""

_IDEMPOTENCY_LOOKUP_SQL = """
    SELECT payable_id, amount
    FROM payable_payments
    WHERE tenant_id = :tid AND idempotency_key = :key
"""

_INSERT_PAYMENT_LEDGER_SQL = """
    INSERT INTO payable_payments (
        tenant_id, payable_id, amount, currency, payment_ref,
        idempotency_key, paid_by_user_id, paid_at
    ) VALUES (
        :tid, :pid, :amount, :ccy, :ref,
        :key, :uid, :paid_at
    )
"""


def _money(value: Decimal | int | str) -> Decimal:
    """Quantise any monetary input to the ledger's 2-dp `NUMERIC(14, 2)` scale."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _idempotency_key(value: str | None) -> str | None:
    """Normalise an idempotency key; an empty string is the same as no key at all."""
    key = (value or "").strip()
    return key or None


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
    idempotency_key: str | None = Field(default=None, max_length=128)
    paid_at: datetime | None = None

    @field_validator("payment_ref")
    @classmethod
    def _strip_ref(cls, value: str) -> str:
        return value.strip()

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, value: str | None) -> str | None:
        return _idempotency_key(value)


class CurrencyCashflowBreakdown(BaseModel):
    """One currency's AR/AP position, so foreign-currency flows are never hidden."""

    currency: str
    ar_total: Decimal
    ar_collected: Decimal
    ar_outstanding: Decimal
    ap_total: Decimal
    ap_paid: Decimal
    ap_outstanding: Decimal
    net_cashflow: Decimal


class CashflowSummaryRead(BaseModel):
    """AR/AP position and net cash flow, all denominated in the tenant base currency.

    The top-level fields stay the base-currency summary (fully backwards compatible),
    while `by_currency` reports every currency actually present in the ledger. Each
    currency is only ever summed against itself - no cross-currency addition.
    """

    currency: str
    ar_total: Decimal
    ar_collected: Decimal
    ar_outstanding: Decimal
    ap_total: Decimal
    ap_paid: Decimal
    ap_outstanding: Decimal
    net_cashflow: Decimal
    by_currency: list[CurrencyCashflowBreakdown] = Field(default_factory=list)


class PayableService:
    """Supplier AP ledger: PO ingestion, L0 settlement and the cash-flow summary."""

    async def sync_purchase_orders(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal | None = None,
    ) -> int:
        """Book an `open` payable for every committed purchase order that has none yet.

        Only POs whose status is `ordered`, `partially_received` or `received` are
        committed spend: `draft` POs are still being edited and `cancelled` POs are
        never a liability. An unpaid payable whose PO is now `draft` or `cancelled` is
        purged (audited), while a partially paid one is kept as an auditable record.

        Idempotent per ``(tenant_id, po_id)``: the ``NOT EXISTS`` guard makes a
        repeated call a no-op, and the advisory lock (released at transaction
        end) stops two racing requests from booking the same PO twice.
        """
        # Advisory lock key: one writer per tenant for the whole sync statement.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
            {"key": f"finance.ap.sync:{tenant_id}"},
        )
        actor_type: Literal["human_user", "system_cron"] = (
            "human_user" if principal is not None else "system_cron"
        )
        actor_id = str(principal.user_id) if principal is not None else "finance.ap_service"

        purged = (
            (await session.execute(text(_PURGE_ORPHAN_PAYABLES_SQL), {"tid": tenant_id}))
            .mappings()
            .all()
        )
        for row in purged:
            await record_audit_event(
                session,
                tenant_id=tenant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action="finance.payable.purged_from_uncommitted_purchase_order",
                resource_type="payable",
                resource_id=row["id"],
                before_state={
                    "po_no": str(row["po_no"]),
                    "po_status": str(row["po_status"]),
                    "status": "open",
                    "paid_amount": "0.00",
                },
                after_state=None,
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
        idempotency_key: str | None = None,
    ) -> PayableRead:
        """Settle a payable (L0 fund red line) and advance ``open -> partial -> paid``.

        Every settlement is written to the immutable `payable_payments` ledger inside
        the same transaction as the `payables.paid_amount` update, keyed by
        ``(tenant_id, idempotency_key)``. A retry with the same key and the same
        ``(payable_id, amount)`` returns the already-updated payable without
        double-counting; the same key with a different amount is a 409 conflict.
        """
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
        key = _idempotency_key(idempotency_key) or _idempotency_key(payload.idempotency_key)
        key = key or _idempotency_key(payload.payment_ref)
        ledger_ready = key is not None and await self._ledger_ready(session)

        if ledger_ready:
            replay = (
                (
                    await session.execute(
                        text(_IDEMPOTENCY_LOOKUP_SQL), {"tid": tenant_id, "key": key}
                    )
                )
                .mappings()
                .first()
            )
            if replay is not None:
                if replay["payable_id"] == payable_id and _money(replay["amount"]) == amount:
                    # Same request replayed: the payable already reflects this payment.
                    return self._to_read(row)
                raise AppError(
                    slug="ap-idempotency-conflict",
                    status_code=409,
                    code="ERR_AP_IDEMPOTENCY_CONFLICT",
                    title="Idempotency Key Conflict",
                    detail=(
                        f"Idempotency key '{key}' was already used for a different "
                        "payable or amount."
                    ),
                )

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

        if ledger_ready:
            try:
                await session.execute(
                    text(_INSERT_PAYMENT_LEDGER_SQL),
                    {
                        "tid": tenant_id,
                        "pid": payable_id,
                        "amount": amount,
                        "ccy": str(row["currency"]).upper(),
                        "ref": payment_ref,
                        "key": key,
                        "uid": principal.user_id,
                        "paid_at": settled_at,
                    },
                )
            except IntegrityError:
                raise AppError(
                    slug="ap-idempotency-conflict",
                    status_code=409,
                    code="ERR_AP_IDEMPOTENCY_CONFLICT",
                    title="Idempotency Key Conflict",
                    detail=(
                        f"Idempotency key '{key}' was already used for a different "
                        "payable or amount."
                    ),
                ) from None

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
                "idempotency_key": key,
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
        by_currency = [
            CurrencyCashflowBreakdown(
                currency=str(entry["ccy"]).strip().upper(),
                ar_total=_money(entry["ar_total"]),
                ar_collected=_money(entry["ar_collected"]),
                ar_outstanding=_money(
                    Decimal(str(entry["ar_total"])) - Decimal(str(entry["ar_collected"]))
                ),
                ap_total=_money(entry["ap_total"]),
                ap_paid=_money(entry["ap_paid"]),
                ap_outstanding=_money(
                    Decimal(str(entry["ap_total"])) - Decimal(str(entry["ap_paid"]))
                ),
                net_cashflow=_money(
                    Decimal(str(entry["ar_collected"])) - Decimal(str(entry["ap_paid"]))
                ),
            )
            for entry in (
                (await session.execute(text(_BY_CURRENCY_SQL), {"tid": tenant_id}))
                .mappings()
                .all()
            )
        ]
        return CashflowSummaryRead(
            currency=currency,
            ar_total=ar_total,
            ar_collected=ar_collected,
            ar_outstanding=_money(ar_total - ar_collected),
            ap_total=ap_total,
            ap_paid=ap_paid,
            ap_outstanding=_money(ap_total - ap_paid),
            net_cashflow=_money(ar_collected - ap_paid),
            by_currency=by_currency,
        )

    @staticmethod
    async def _ledger_ready(session: AsyncSession) -> bool:
        """True once migration 0013 created `payable_payments`.

        The ledger lands in a later revision than the rest of the AP module, so a
        database that has not caught up yet still settles payables - it simply cannot
        deduplicate retries until the table exists. `to_regclass` is a cheap catalog
        probe and never raises for a missing relation.
        """
        present = await session.scalar(text("SELECT to_regclass('public.payable_payments')"))
        return present is not None

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
