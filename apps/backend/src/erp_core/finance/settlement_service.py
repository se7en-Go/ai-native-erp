"""Dual-basis profit reconciliation, settlement import, payout matching, journal export.

Two accounting bases are reconciled here, and nothing in this module is a float:

* **Order-estimated basis** - `order_profit_snapshots`, the CVP contribution
  margin an order promised when it was confirmed (gross revenue, commission,
  shipping & duty, ad attribution, COGS).
* **Settlement-actual basis** - `settlement_lines`, what the marketplace really
  remitted after commission, fulfillment, storage, advertising and refunds,
  converted to the tenant base currency with the statement's locked FX rate,
  minus the same COGS, plus the realised FX variance of the matched payouts.

Every amount is `decimal.Decimal` quantised with `ROUND_HALF_UP` to the cent
(and to 6 dp for FX rates) and persisted as PostgreSQL `NUMERIC`. The journal
export additionally proves the double-entry invariant `sum(debits) ==
sum(credits)` before a single CSV byte is returned.

Tenancy: every statement is scoped by `tenant_id = :tid` inside a
`tenant_session`, with the RESTRICTIVE RLS policy as the second line of defence.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import RowMapping, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.schemas import ExactDecimal
from erp_core.finance.settlement_models import PayoutStatement, SettlementBatch, SettlementLine
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal

#: Roles allowed to read settlements, payouts, dual-basis profit and journal CSVs.
SETTLEMENT_READ_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.FINANCE,
)
#: Roles allowed to import statements and mutate payout reconciliation state.
SETTLEMENT_WRITE_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.FINANCE,
    UserRole.ADMIN,
)

#: Providers a payout statement may come from.
PAYOUT_PROVIDERS: tuple[str, ...] = (
    "pingpong",
    "lianlian",
    "payoneer",
    "bank_tt",
    "stripe",
    "other",
)
#: Journal export dialects.
JOURNAL_FORMATS: tuple[str, ...] = ("standard", "quickbooks", "xero", "kingdee")

#: Absolute tolerance (statement currency) accepted by the amount-based matcher.
MATCH_TOLERANCE = Decimal("2.00")

SETTLEMENT_COLUMNS: tuple[str, ...] = (
    "external_order_id",
    "sku",
    "gross_sales_ccy",
    "commission_ccy",
    "fulfillment_fee_ccy",
    "storage_fee_ccy",
    "ad_fee_ccy",
    "refund_ccy",
)
SETTLEMENT_OPTIONAL_COLUMNS: tuple[str, ...] = ("net_settlement_ccy",)

PAYOUT_COLUMNS: tuple[str, ...] = (
    "external_tx_ref",
    "payout_currency",
    "payout_amount",
    "settled_base_currency",
    "settled_base_amount",
    "expected_fx_rate",
)
PAYOUT_OPTIONAL_COLUMNS: tuple[str, ...] = ("actual_fx_rate", "paid_at", "reference_hint")

#: Numeric-only cell shape: a value matching it can never be a CSV formula.
_NUMERIC_CELL = re.compile(r"^-?\d+(\.\d{1,6})?$")
#: Characters that make Excel/Sheets/Kingdee treat a cell as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# --------------------------------------------------------------------------- #
# Account chart used by the voucher export
# --------------------------------------------------------------------------- #
ACC_CASH = ("1000", "Cash / Bank")
ACC_AR = ("1100", "Accounts Receivable")
ACC_CLEARING = ("1110", "Marketplace Clearing")
ACC_INVENTORY = ("1200", "Inventory")
ACC_REVENUE = ("4000", "Sales Revenue")
ACC_RETURNS = ("4100", "Sales Returns")
ACC_COGS = ("5000", "Cost of Goods Sold")
ACC_COMMISSION = ("6100", "Platform Commission Expense")
ACC_FULFILLMENT = ("6200", "Fulfillment & Storage Expense")
ACC_ADVERTISING = ("6300", "Advertising Expense")
ACC_FX = ("7000", "FX Gain / Loss")


def _q2(value: Decimal) -> Decimal:
    """Quantise a money amount to the cent with `ROUND_HALF_UP`."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q6(value: Decimal) -> Decimal:
    """Quantise an FX rate to 6 dp with `ROUND_HALF_UP`."""
    return Decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _zero(value: Decimal) -> Decimal:
    """Normalise `-0.00` to `0.00` so CSV output and equality checks stay stable."""
    return Decimal("0.00") if value == 0 else value


def _money(value: Decimal) -> str:
    """Canonical two-decimal string for CSV output."""
    return str(_zero(_q2(value)))


def _rate(value: Decimal) -> str:
    """Canonical six-decimal FX string for CSV output."""
    return str(_q6(value))


def _as_decimal(value: object, field_name: str) -> Decimal:
    """Exact decimal coercion; a `float`/`bool` is a hard error, never silently rounded."""
    if isinstance(value, (bool, float)):
        raise AppError(
            slug="invalid-decimal-value",
            status_code=422,
            code="ERR_INVALID_DECIMAL",
            title="Invalid Decimal Value",
            detail=f"'{field_name}' must be an exact decimal string, never a JSON float.",
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise AppError(
                slug="invalid-decimal-value",
                status_code=422,
                code="ERR_INVALID_DECIMAL",
                title="Invalid Decimal Value",
                detail=f"'{field_name}' is not a valid decimal: {value!r}.",
            ) from exc
    raise AppError(
        slug="invalid-decimal-value",
        status_code=422,
        code="ERR_INVALID_DECIMAL",
        title="Invalid Decimal Value",
        detail=f"'{field_name}' must be a decimal string.",
    )


def _text(value: object) -> str:
    """Trimmed text cell; `None`/blank becomes an empty string."""
    if value is None:
        return ""
    return str(value).strip()


def csv_safe(value: str) -> str:
    """Neutralise CSV formula injection without corrupting genuine numbers.

    A negative money amount (`-120.00`) must survive untouched or the accounting
    import breaks, so a cell matching the numeric shape is always emitted as-is.
    Any other cell that starts with `=`, `+`, `-`, `@`, TAB or CR gets a leading
    apostrophe, which Excel/Sheets/Kingdee render as literal text.
    """
    if _NUMERIC_CELL.match(value):
        return value
    if value[:1] in _FORMULA_PREFIXES:
        return f"'{value}"
    return value


def _write_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render a CSV document with every cell passed through the formula guard."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([csv_safe(value) for value in header])
    for row in rows:
        writer.writerow([csv_safe(value) for value in row])
    return buf.getvalue()


def _bad_csv(detail: str) -> AppError:
    return AppError(
        slug="invalid-csv-payload",
        status_code=422,
        code="ERR_INVALID_CSV",
        title="Invalid CSV Payload",
        detail=detail,
    )


def _csv_rows(
    csv_text: str, required: Sequence[str], optional: Sequence[str]
) -> list[dict[str, str]]:
    """Parse a CSV document whose header must cover `required` (and may cover `optional`)."""
    stripped = csv_text.lstrip("\ufeff").strip()
    if not stripped:
        raise _bad_csv("The CSV payload is empty.")
    reader = csv.DictReader(io.StringIO(stripped))
    header = [name.strip() for name in (reader.fieldnames or [])]
    missing = [name for name in required if name not in header]
    if missing:
        raise _bad_csv(f"The CSV header is missing required columns: {', '.join(missing)}.")
    unknown = [name for name in header if name not in (*required, *optional)]
    if unknown:
        raise _bad_csv(f"The CSV header carries unsupported columns: {', '.join(unknown)}.")
    reader.fieldnames = header
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = {name: _text(raw_row.get(name)) for name in header}
        if not any(row.values()):
            continue
        rows.append(row)
    if not rows:
        raise _bad_csv("The CSV payload contains no data rows.")
    return rows


def _row_decimal(row: Mapping[str, str], key: str, *, default: Decimal | None = None) -> Decimal:
    raw = row.get(key, "")
    if raw == "":
        return Decimal("0.00") if default is None else default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise _bad_csv(f"Column '{key}' carries a non-numeric value: {raw!r}.") from exc


# --------------------------------------------------------------------------- #
# Import payload contracts
# --------------------------------------------------------------------------- #
class SettlementLineInput(BaseModel):
    """One marketplace settlement row, either from JSON or parsed from CSV."""

    external_order_id: str | None = Field(default=None, max_length=128)
    sku: str | None = Field(default=None, max_length=128)
    gross_sales_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    commission_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    fulfillment_fee_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    storage_fee_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    ad_fee_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    refund_ccy: ExactDecimal = Field(default=Decimal("0.00"))
    net_settlement_ccy: ExactDecimal | None = Field(default=None)

    @field_validator("external_order_id", "sku")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class SettlementImportRequest(BaseModel):
    """Marketplace payout statement header plus its rows (JSON list or CSV text)."""

    channel_type: str = Field(min_length=1, max_length=64)
    batch_ref: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern="^[A-Za-z]{3}$")
    period_start: date
    period_end: date
    fx_rate_to_base: ExactDecimal = Field(gt=Decimal("0"), max_digits=12, decimal_places=6)
    channel_account_id: UUID | None = None
    lines: list[SettlementLineInput] = Field(default_factory=list)
    csv_text: str | None = None

    @field_validator("period_end")
    @classmethod
    def _period_order(cls, value: date, info: ValidationInfo) -> date:
        start = info.data.get("period_start")
        if isinstance(start, date) and value < start:
            raise ValueError("period_end must not precede period_start")
        return value


class PayoutStatementInput(BaseModel):
    """One cross-border collection statement row."""

    external_tx_ref: str = Field(min_length=1, max_length=128)
    payout_currency: str = Field(pattern="^[A-Za-z]{3}$")
    payout_amount: ExactDecimal = Field(gt=Decimal("0"), max_digits=14, decimal_places=2)
    settled_base_currency: str = Field(pattern="^[A-Za-z]{3}$")
    settled_base_amount: ExactDecimal = Field(max_digits=14, decimal_places=2)
    expected_fx_rate: ExactDecimal | None = Field(
        default=None, gt=Decimal("0"), max_digits=12, decimal_places=6
    )
    actual_fx_rate: ExactDecimal | None = Field(
        default=None, gt=Decimal("0"), max_digits=12, decimal_places=6
    )
    paid_at: datetime | None = None
    reference_hint: str | None = Field(default=None, max_length=128)


class PayoutImportRequest(BaseModel):
    """Payout statement import: a provider label plus JSON rows or CSV text."""

    provider: Literal["pingpong", "lianlian", "payoneer", "bank_tt", "stripe", "other"]
    rows: list[PayoutStatementInput] = Field(default_factory=list)
    csv_text: str | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def _lower_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


# --------------------------------------------------------------------------- #
# Read contracts
# --------------------------------------------------------------------------- #
class SettlementBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    channel_type: str
    channel_account_id: UUID | None
    batch_ref: str
    currency: str
    period_start: date
    period_end: date
    gross_sales_ccy: Decimal
    commission_ccy: Decimal
    fulfillment_fee_ccy: Decimal
    storage_fee_ccy: Decimal
    ad_fee_ccy: Decimal
    refund_ccy: Decimal
    net_payout_ccy: Decimal
    fx_rate_to_base: Decimal
    net_payout_base: Decimal
    status: str
    line_count: int
    created_at: datetime


class SettlementBatchListRead(BaseModel):
    items: list[SettlementBatchRead]
    total_count: int
    total_net_payout_base: Decimal
    base_currency: str


class SettlementLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    batch_id: UUID
    external_order_id: str | None
    order_id: UUID | None
    sku: str | None
    gross_sales_ccy: Decimal
    commission_ccy: Decimal
    fulfillment_fee_ccy: Decimal
    storage_fee_ccy: Decimal
    ad_fee_ccy: Decimal
    refund_ccy: Decimal
    net_settlement_ccy: Decimal
    fx_rate_to_base: Decimal
    net_settlement_base: Decimal
    created_at: datetime


class SettlementLineListRead(BaseModel):
    items: list[SettlementLineRead]
    total_count: int
    matched_count: int
    batch_id: UUID
    currency: str


class PayoutStatementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    provider: str
    external_tx_ref: str
    payout_currency: str
    payout_amount: Decimal
    settled_base_currency: str
    settled_base_amount: Decimal
    actual_fx_rate: Decimal
    expected_fx_rate: Decimal
    fx_variance_base: Decimal
    matched_settlement_batch_id: UUID | None
    matched_order_id: UUID | None
    match_status: str
    paid_at: datetime
    created_at: datetime


class PayoutStatementListRead(BaseModel):
    items: list[PayoutStatementRead]
    total_count: int
    matched_count: int
    unmatched_count: int
    total_fx_variance_base: Decimal


class PayoutImportResultRead(BaseModel):
    imported_count: int
    skipped_count: int
    matched_count: int
    items: list[PayoutStatementRead]


class PayoutAutoMatchResultRead(BaseModel):
    matched_count: int
    remaining_unmatched_count: int
    total_fx_variance_base: Decimal
    items: list[PayoutStatementRead]


class DualBasisOrderRow(BaseModel):
    """One order compared side by side: estimated basis versus settled basis."""

    order_id: UUID
    order_no: str
    currency: str
    estimated_revenue_base: Decimal
    estimated_commission_base: Decimal
    estimated_fulfillment_storage_base: Decimal
    estimated_ad_spend_base: Decimal
    estimated_cogs_base: Decimal
    estimated_net_profit_base: Decimal
    settled_revenue_base: Decimal
    settled_commission_base: Decimal
    settled_fulfillment_storage_base: Decimal
    settled_ad_spend_base: Decimal
    settled_cogs_base: Decimal
    settled_payout_base: Decimal
    fx_variance_base: Decimal
    settled_net_profit_base: Decimal
    commission_delta_base: Decimal
    fulfillment_storage_delta_base: Decimal
    ad_delta_base: Decimal
    profit_variance_base: Decimal


class DualBasisProfitRead(BaseModel):
    base_currency: str
    estimated_revenue_base: Decimal
    estimated_net_margin_base: Decimal
    settled_payout_base: Decimal
    settled_net_profit_base: Decimal
    commission_delta_base: Decimal
    fulfillment_storage_delta_base: Decimal
    ad_spend_base: Decimal
    fx_variance_base: Decimal
    total_profit_variance_base: Decimal
    matched_orders_count: int
    unmatched_lines_count: int
    rows: list[DualBasisOrderRow]


@dataclass(frozen=True)
class JournalEntry:
    """One balanced double-entry line of the voucher export."""

    voucher_no: str
    entry_date: date
    currency: str
    account_code: str
    account_name: str
    description: str
    debit: Decimal
    credit: Decimal


def _entry(
    voucher_no: str,
    entry_date: date,
    currency: str,
    account: tuple[str, str],
    description: str,
    *,
    debit: Decimal = Decimal("0.00"),
    credit: Decimal = Decimal("0.00"),
) -> JournalEntry:
    return JournalEntry(
        voucher_no=voucher_no,
        entry_date=entry_date,
        currency=currency,
        account_code=account[0],
        account_name=account[1],
        description=description,
        debit=_zero(_q2(debit)),
        credit=_zero(_q2(credit)),
    )


class SettlementService:
    """Marketplace settlement importer, dual-basis reporter, and voucher exporter."""

    # ---------------------------------------------------------------- helpers
    async def _base_currency(self, session: AsyncSession, tenant_id: UUID) -> str:
        base = await session.scalar(
            text("SELECT base_currency FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )
        return str(base) if base else "CNY"

    async def _order_lookup(
        self, session: AsyncSession, tenant_id: UUID, references: Iterable[str]
    ) -> dict[str, UUID]:
        """Map `external_order_id` and `order_no` of this tenant onto `orders.id`."""
        wanted = sorted({ref for ref in references if ref})
        if not wanted:
            return {}
        statement = text(
            """
            SELECT id, order_no, external_order_id
            FROM orders
            WHERE tenant_id = :tid
              AND (external_order_id IN :refs_ext OR order_no IN :refs_no)
            ORDER BY created_at
            """
        ).bindparams(
            bindparam("refs_ext", expanding=True),
            bindparam("refs_no", expanding=True),
        )
        rows = (
            await session.execute(
                statement,
                {"tid": tenant_id, "refs_ext": wanted, "refs_no": wanted},
            )
        ).all()
        lookup: dict[str, UUID] = {}
        for row in rows:
            if row.external_order_id:
                lookup.setdefault(str(row.external_order_id), row.id)
            lookup.setdefault(str(row.order_no), row.id)
        return lookup

    @staticmethod
    def _assert_writer(principal: CurrentPrincipal, tenant_id: UUID, action: str) -> None:
        if principal.tenant_id != tenant_id or principal.role not in SETTLEMENT_WRITE_ROLES:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title=f"{action} Forbidden",
                detail=(
                    f"Role '{principal.role.value}' is not authorized to {action.lower()}; "
                    "OWNER, FINANCE or ADMIN is required."
                ),
            )

    # ------------------------------------------------------- settlement import
    def _resolve_settlement_lines(
        self, payload: SettlementImportRequest
    ) -> list[SettlementLineInput]:
        rows = list(payload.lines)
        if payload.csv_text:
            if rows:
                raise AppError(
                    slug="ambiguous-settlement-import",
                    status_code=422,
                    code="ERR_AMBIGUOUS_SETTLEMENT_IMPORT",
                    title="Ambiguous Settlement Import",
                    detail="Supply either structured `lines` or `csv_text`, never both.",
                )
            rows = self._parse_settlement_csv(payload.csv_text)
        if not rows:
            raise AppError(
                slug="empty-settlement-import",
                status_code=422,
                code="ERR_EMPTY_SETTLEMENT_IMPORT",
                title="Empty Settlement Import",
                detail="The settlement import carries no lines.",
            )
        return rows

    @staticmethod
    def _parse_settlement_csv(csv_text: str) -> list[SettlementLineInput]:
        raw_rows = _csv_rows(csv_text, SETTLEMENT_COLUMNS, SETTLEMENT_OPTIONAL_COLUMNS)
        parsed: list[SettlementLineInput] = []
        for row in raw_rows:
            net_raw = row.get("net_settlement_ccy", "")
            parsed.append(
                SettlementLineInput(
                    external_order_id=row.get("external_order_id") or None,
                    sku=row.get("sku") or None,
                    gross_sales_ccy=_row_decimal(row, "gross_sales_ccy"),
                    commission_ccy=_row_decimal(row, "commission_ccy"),
                    fulfillment_fee_ccy=_row_decimal(row, "fulfillment_fee_ccy"),
                    storage_fee_ccy=_row_decimal(row, "storage_fee_ccy"),
                    ad_fee_ccy=_row_decimal(row, "ad_fee_ccy"),
                    refund_ccy=_row_decimal(row, "refund_ccy"),
                    net_settlement_ccy=(
                        _row_decimal(row, "net_settlement_ccy") if net_raw else None
                    ),
                )
            )
        return parsed

    async def import_settlements(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: SettlementImportRequest,
    ) -> SettlementBatchRead:
        """Import one marketplace statement; idempotent per `(tenant, channel, batch_ref)`."""
        self._assert_writer(principal, tenant_id, "Import Settlements")
        rows = self._resolve_settlement_lines(payload)
        currency = payload.currency.upper()
        fx = _q6(_as_decimal(payload.fx_rate_to_base, "fx_rate_to_base"))

        existing = (
            await session.execute(
                select(SettlementBatch).where(
                    SettlementBatch.tenant_id == tenant_id,
                    SettlementBatch.channel_type == payload.channel_type,
                    SettlementBatch.batch_ref == payload.batch_ref,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await self._batch_read(session, tenant_id, existing)

        lookup = await self._order_lookup(
            session,
            tenant_id,
            (row.external_order_id or "" for row in rows),
        )

        totals = {
            "gross": Decimal("0.00"),
            "commission": Decimal("0.00"),
            "fulfillment": Decimal("0.00"),
            "storage": Decimal("0.00"),
            "ad": Decimal("0.00"),
            "refund": Decimal("0.00"),
            "net": Decimal("0.00"),
        }
        batch_id = uuid4()
        line_models: list[SettlementLine] = []
        line_ids: list[UUID] = []
        for row in rows:
            gross = _q2(row.gross_sales_ccy)
            commission = _q2(row.commission_ccy)
            fulfillment = _q2(row.fulfillment_fee_ccy)
            storage = _q2(row.storage_fee_ccy)
            ad = _q2(row.ad_fee_ccy)
            refund = _q2(row.refund_ccy)
            if row.net_settlement_ccy is None:
                net = _q2(gross - commission - fulfillment - storage - ad - refund)
            else:
                net = _q2(row.net_settlement_ccy)
            net_base = _q2(net * fx)

            totals["gross"] += gross
            totals["commission"] += commission
            totals["fulfillment"] += fulfillment
            totals["storage"] += storage
            totals["ad"] += ad
            totals["refund"] += refund
            totals["net"] += net

            model = SettlementLine(
                id=uuid4(),
                tenant_id=tenant_id,
                batch_id=batch_id,
                external_order_id=row.external_order_id,
                order_id=lookup.get(row.external_order_id or ""),
                sku=row.sku,
                gross_sales_ccy=gross,
                commission_ccy=commission,
                fulfillment_fee_ccy=fulfillment,
                storage_fee_ccy=storage,
                ad_fee_ccy=ad,
                refund_ccy=refund,
                net_settlement_ccy=net,
                fx_rate_to_base=fx,
                net_settlement_base=net_base,
            )
            line_models.append(model)
            line_ids.append(model.id)

        batch = SettlementBatch(
            id=batch_id,
            tenant_id=tenant_id,
            channel_type=payload.channel_type,
            channel_account_id=payload.channel_account_id,
            batch_ref=payload.batch_ref,
            currency=currency,
            period_start=payload.period_start,
            period_end=payload.period_end,
            gross_sales_ccy=_q2(totals["gross"]),
            commission_ccy=_q2(totals["commission"]),
            fulfillment_fee_ccy=_q2(totals["fulfillment"]),
            storage_fee_ccy=_q2(totals["storage"]),
            ad_fee_ccy=_q2(totals["ad"]),
            refund_ccy=_q2(totals["refund"]),
            net_payout_ccy=_q2(totals["net"]),
            fx_rate_to_base=fx,
            status="imported",
        )
        session.add(batch)
        await session.flush()
        for model in line_models:
            session.add(model)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="finance.settlement_batch.imported",
            resource_type="settlement_batch",
            resource_id=batch.id,
            after_state={
                "batch_ref": batch.batch_ref,
                "channel_type": batch.channel_type,
                "currency": batch.currency,
                "line_count": len(line_models),
                "gross_sales_ccy": _money(batch.gross_sales_ccy),
                "net_payout_ccy": _money(batch.net_payout_ccy),
                "fx_rate_to_base": _rate(batch.fx_rate_to_base),
                "matched_line_ids": [str(line_id) for line_id in line_ids],
            },
        )
        await session.refresh(batch)
        return await self._batch_read(session, tenant_id, batch)

    async def _batch_read(
        self, session: AsyncSession, tenant_id: UUID, batch: SettlementBatch
    ) -> SettlementBatchRead:
        line_count = await session.scalar(
            text(
                "SELECT COUNT(*) FROM settlement_lines WHERE tenant_id = :tid AND batch_id = :bid"
            ),
            {"tid": tenant_id, "bid": batch.id},
        )
        fx = _q6(_as_decimal(batch.fx_rate_to_base, "fx_rate_to_base"))
        return SettlementBatchRead(
            id=batch.id,
            tenant_id=batch.tenant_id,
            channel_type=batch.channel_type,
            channel_account_id=batch.channel_account_id,
            batch_ref=batch.batch_ref,
            currency=batch.currency,
            period_start=batch.period_start,
            period_end=batch.period_end,
            gross_sales_ccy=_q2(batch.gross_sales_ccy),
            commission_ccy=_q2(batch.commission_ccy),
            fulfillment_fee_ccy=_q2(batch.fulfillment_fee_ccy),
            storage_fee_ccy=_q2(batch.storage_fee_ccy),
            ad_fee_ccy=_q2(batch.ad_fee_ccy),
            refund_ccy=_q2(batch.refund_ccy),
            net_payout_ccy=_q2(batch.net_payout_ccy),
            fx_rate_to_base=fx,
            net_payout_base=_q2(Decimal(batch.net_payout_ccy) * fx),
            status=batch.status,
            line_count=int(line_count or 0),
            created_at=batch.created_at,
        )

    async def list_settlements(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        channel_type: str | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SettlementBatchListRead:
        clauses = ["tenant_id = :tid"]
        filter_params: dict[str, object] = {"tid": tenant_id}
        if channel_type:
            clauses.append("channel_type = :channel_type")
            filter_params["channel_type"] = channel_type
        if status_filter:
            clauses.append("status = :status")
            filter_params["status"] = status_filter
        where_sql = " AND ".join(clauses)

        rows = (
            (
                await session.execute(
                    text(
                        f"""
                    SELECT b.*,
                           (SELECT COUNT(*) FROM settlement_lines l
                             WHERE l.tenant_id = b.tenant_id AND l.batch_id = b.id)
                               AS line_count
                    FROM settlement_batches b
                    WHERE {where_sql}
                    ORDER BY b.period_end DESC, b.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                    ),
                    {**filter_params, "limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
        total = await session.scalar(
            text(f"SELECT COUNT(*) FROM settlement_batches WHERE {where_sql}"), filter_params
        )
        base_currency = await self._base_currency(session, tenant_id)
        items = [self._batch_row_to_read(row) for row in rows]
        total_net = Decimal("0.00")
        for item in items:
            total_net += item.net_payout_base
        return SettlementBatchListRead(
            items=items,
            total_count=int(total or 0),
            total_net_payout_base=_q2(total_net),
            base_currency=base_currency,
        )

    @staticmethod
    def _batch_row_to_read(row: RowMapping) -> SettlementBatchRead:
        fx = _q6(_as_decimal(row["fx_rate_to_base"], "fx_rate_to_base"))
        net_ccy = _q2(_as_decimal(row["net_payout_ccy"], "net_payout_ccy"))
        return SettlementBatchRead(
            id=cast(UUID, row["id"]),
            tenant_id=cast(UUID, row["tenant_id"]),
            channel_type=str(row["channel_type"]),
            channel_account_id=cast(UUID | None, row["channel_account_id"]),
            batch_ref=str(row["batch_ref"]),
            currency=str(row["currency"]),
            period_start=cast(date, row["period_start"]),
            period_end=cast(date, row["period_end"]),
            gross_sales_ccy=_q2(_as_decimal(row["gross_sales_ccy"], "gross_sales_ccy")),
            commission_ccy=_q2(_as_decimal(row["commission_ccy"], "commission_ccy")),
            fulfillment_fee_ccy=_q2(_as_decimal(row["fulfillment_fee_ccy"], "fulfillment_fee_ccy")),
            storage_fee_ccy=_q2(_as_decimal(row["storage_fee_ccy"], "storage_fee_ccy")),
            ad_fee_ccy=_q2(_as_decimal(row["ad_fee_ccy"], "ad_fee_ccy")),
            refund_ccy=_q2(_as_decimal(row["refund_ccy"], "refund_ccy")),
            net_payout_ccy=net_ccy,
            fx_rate_to_base=fx,
            net_payout_base=_q2(net_ccy * fx),
            status=str(row["status"]),
            line_count=int(cast(int, row["line_count"] or 0)),
            created_at=cast(datetime, row["created_at"]),
        )

    async def list_settlement_lines(
        self, session: AsyncSession, tenant_id: UUID, batch_id: UUID
    ) -> SettlementLineListRead:
        batch = (
            await session.execute(
                select(SettlementBatch).where(
                    SettlementBatch.tenant_id == tenant_id,
                    SettlementBatch.id == batch_id,
                )
            )
        ).scalar_one_or_none()
        if batch is None:
            raise AppError(
                slug="settlement-batch-not-found",
                status_code=404,
                code="ERR_SETTLEMENT_BATCH_NOT_FOUND",
                title="Settlement Batch Not Found",
                detail=f"Settlement batch '{batch_id}' not found.",
            )
        rows = (
            (
                await session.execute(
                    select(SettlementLine)
                    .where(
                        SettlementLine.tenant_id == tenant_id,
                        SettlementLine.batch_id == batch_id,
                    )
                    .order_by(SettlementLine.created_at, SettlementLine.id)
                )
            )
            .scalars()
            .all()
        )
        items = [
            SettlementLineRead(
                id=row.id,
                tenant_id=row.tenant_id,
                batch_id=row.batch_id,
                external_order_id=row.external_order_id,
                order_id=row.order_id,
                sku=row.sku,
                gross_sales_ccy=_q2(row.gross_sales_ccy),
                commission_ccy=_q2(row.commission_ccy),
                fulfillment_fee_ccy=_q2(row.fulfillment_fee_ccy),
                storage_fee_ccy=_q2(row.storage_fee_ccy),
                ad_fee_ccy=_q2(row.ad_fee_ccy),
                refund_ccy=_q2(row.refund_ccy),
                net_settlement_ccy=_q2(row.net_settlement_ccy),
                fx_rate_to_base=_q6(row.fx_rate_to_base),
                net_settlement_base=_q2(row.net_settlement_base),
                created_at=row.created_at,
            )
            for row in rows
        ]
        return SettlementLineListRead(
            items=items,
            total_count=len(items),
            matched_count=sum(1 for item in items if item.order_id is not None),
            batch_id=batch_id,
            currency=batch.currency,
        )

    # ------------------------------------------------------ dual-basis profit
    async def get_dual_basis_profit(
        self, session: AsyncSession, tenant_id: UUID
    ) -> DualBasisProfitRead:
        """Compare the estimated CVP basis against the settled marketplace basis."""
        rows = (
            (
                await session.execute(
                    text(
                        """
                    WITH settled AS (
                        SELECT
                            l.order_id,
                            COALESCE(SUM(l.gross_sales_ccy * l.fx_rate_to_base), 0)
                                AS gross_base,
                            COALESCE(SUM(l.refund_ccy * l.fx_rate_to_base), 0)
                                AS refund_base,
                            COALESCE(SUM(l.commission_ccy * l.fx_rate_to_base), 0)
                                AS commission_base,
                            COALESCE(SUM(
                                (l.fulfillment_fee_ccy + l.storage_fee_ccy)
                                * l.fx_rate_to_base
                            ), 0) AS fulfillment_storage_base,
                            COALESCE(SUM(l.ad_fee_ccy * l.fx_rate_to_base), 0) AS ad_base,
                            COALESCE(SUM(l.net_settlement_base), 0) AS net_settlement_base
                        FROM settlement_lines l
                        WHERE l.tenant_id = :tid AND l.order_id IS NOT NULL
                        GROUP BY l.order_id
                    ),
                    fx AS (
                        SELECT p.matched_order_id AS order_id,
                               COALESCE(SUM(p.fx_variance_base), 0) AS fx_variance_base
                        FROM payout_statements p
                        WHERE p.tenant_id = :tid AND p.matched_order_id IS NOT NULL
                        GROUP BY p.matched_order_id
                    )
                    SELECT
                        o.id AS order_id,
                        o.order_no,
                        o.currency,
                        s.gross_revenue_base,
                        s.platform_commission_base,
                        s.shipping_and_duty_base,
                        s.ad_attribution_cost_base,
                        s.cogs_base,
                        s.net_contribution_margin_base,
                        settled.gross_base,
                        settled.refund_base,
                        settled.commission_base,
                        settled.fulfillment_storage_base,
                        settled.ad_base,
                        settled.net_settlement_base,
                        COALESCE(fx.fx_variance_base, 0) AS fx_variance_base
                    FROM settled
                    JOIN orders o
                      ON o.id = settled.order_id AND o.tenant_id = :tid
                    JOIN order_profit_snapshots s
                      ON s.order_id = settled.order_id AND s.tenant_id = :tid
                    LEFT JOIN fx ON fx.order_id = settled.order_id
                    ORDER BY o.order_no
                    """
                    ),
                    {"tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )

        unmatched_lines_count = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM settlement_lines "
                    "WHERE tenant_id = :tid AND order_id IS NULL"
                ),
                {"tid": tenant_id},
            )
            or 0
        )

        order_rows: list[DualBasisOrderRow] = []
        agg_estimated_revenue = Decimal("0.00")
        agg_estimated_net = Decimal("0.00")
        agg_settled_payout = Decimal("0.00")
        agg_settled_net = Decimal("0.00")
        agg_commission_delta = Decimal("0.00")
        agg_fulfillment_delta = Decimal("0.00")
        agg_ad_spend = Decimal("0.00")
        agg_fx_variance = Decimal("0.00")
        for row in rows:
            estimated_revenue = _q2(_as_decimal(row["gross_revenue_base"], "gross_revenue_base"))
            estimated_commission = _q2(
                _as_decimal(row["platform_commission_base"], "platform_commission_base")
            )
            estimated_fulfillment = _q2(
                _as_decimal(row["shipping_and_duty_base"], "shipping_and_duty_base")
            )
            estimated_ad = _q2(
                _as_decimal(row["ad_attribution_cost_base"], "ad_attribution_cost_base")
            )
            estimated_cogs = _q2(_as_decimal(row["cogs_base"], "cogs_base"))
            estimated_net = _q2(
                _as_decimal(row["net_contribution_margin_base"], "net_contribution_margin_base")
            )
            settled_revenue = _q2(
                _as_decimal(row["gross_base"], "gross_base")
                - _as_decimal(row["refund_base"], "refund_base")
            )
            settled_commission = _q2(_as_decimal(row["commission_base"], "commission_base"))
            settled_fulfillment = _q2(
                _as_decimal(row["fulfillment_storage_base"], "fulfillment_storage_base")
            )
            settled_ad = _q2(_as_decimal(row["ad_base"], "ad_base"))
            settled_payout = _q2(_as_decimal(row["net_settlement_base"], "net_settlement_base"))
            fx_variance = _q2(_as_decimal(row["fx_variance_base"], "fx_variance_base"))
            settled_net = _q2(settled_payout - estimated_cogs + fx_variance)

            order_rows.append(
                DualBasisOrderRow(
                    order_id=row["order_id"],
                    order_no=str(row["order_no"]),
                    currency=str(row["currency"]),
                    estimated_revenue_base=estimated_revenue,
                    estimated_commission_base=estimated_commission,
                    estimated_fulfillment_storage_base=estimated_fulfillment,
                    estimated_ad_spend_base=estimated_ad,
                    estimated_cogs_base=estimated_cogs,
                    estimated_net_profit_base=estimated_net,
                    settled_revenue_base=settled_revenue,
                    settled_commission_base=settled_commission,
                    settled_fulfillment_storage_base=settled_fulfillment,
                    settled_ad_spend_base=settled_ad,
                    settled_cogs_base=estimated_cogs,
                    settled_payout_base=settled_payout,
                    fx_variance_base=fx_variance,
                    settled_net_profit_base=settled_net,
                    commission_delta_base=_q2(settled_commission - estimated_commission),
                    fulfillment_storage_delta_base=_q2(settled_fulfillment - estimated_fulfillment),
                    ad_delta_base=_q2(settled_ad - estimated_ad),
                    profit_variance_base=_q2(settled_net - estimated_net),
                )
            )

            agg_estimated_revenue += estimated_revenue
            agg_estimated_net += estimated_net
            agg_settled_payout += settled_payout
            agg_settled_net += settled_net
            agg_commission_delta += settled_commission - estimated_commission
            agg_fulfillment_delta += settled_fulfillment - estimated_fulfillment
            agg_ad_spend += settled_ad
            agg_fx_variance += fx_variance

        base_currency = await self._base_currency(session, tenant_id)
        return DualBasisProfitRead(
            base_currency=base_currency,
            estimated_revenue_base=_q2(agg_estimated_revenue),
            estimated_net_margin_base=_q2(agg_estimated_net),
            settled_payout_base=_q2(agg_settled_payout),
            settled_net_profit_base=_q2(agg_settled_net),
            commission_delta_base=_q2(agg_commission_delta),
            fulfillment_storage_delta_base=_q2(agg_fulfillment_delta),
            ad_spend_base=_q2(agg_ad_spend),
            fx_variance_base=_q2(agg_fx_variance),
            total_profit_variance_base=_q2(agg_settled_net - agg_estimated_net),
            matched_orders_count=len(order_rows),
            unmatched_lines_count=unmatched_lines_count,
            rows=order_rows,
        )

    # -------------------------------------------------------- payout matching
    async def import_payouts(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: PayoutImportRequest,
    ) -> PayoutImportResultRead:
        """Import provider statements, compute FX rate + variance, then auto-match hints."""
        self._assert_writer(principal, tenant_id, "Import Payouts")
        rows = list(payload.rows)
        if payload.csv_text:
            if rows:
                raise AppError(
                    slug="ambiguous-payout-import",
                    status_code=422,
                    code="ERR_AMBIGUOUS_PAYOUT_IMPORT",
                    title="Ambiguous Payout Import",
                    detail="Supply either structured `rows` or `csv_text`, never both.",
                )
            rows = self._parse_payout_csv(payload.csv_text)
        if not rows:
            raise AppError(
                slug="empty-payout-import",
                status_code=422,
                code="ERR_EMPTY_PAYOUT_IMPORT",
                title="Empty Payout Import",
                detail="The payout import carries no statements.",
            )

        known: set[tuple[str, str]] = {
            (str(row[0]), str(row[1]))
            for row in (
                await session.execute(
                    text(
                        "SELECT provider, external_tx_ref FROM payout_statements "
                        "WHERE tenant_id = :tid AND provider = :provider"
                    ),
                    {"tid": tenant_id, "provider": payload.provider},
                )
            ).all()
        }
        imported: list[PayoutStatement] = []
        skipped = 0
        now = datetime.now().astimezone()
        for row in rows:
            if (payload.provider, row.external_tx_ref) in known:
                skipped += 1
                continue
            payout_amount = _q2(row.payout_amount)
            settled_amount = _q2(row.settled_base_amount)
            actual = (
                _q6(row.actual_fx_rate)
                if row.actual_fx_rate is not None
                else _q6(settled_amount / payout_amount)
            )
            expected = _q6(row.expected_fx_rate) if row.expected_fx_rate is not None else actual
            variance = _q2(settled_amount - (payout_amount * expected))
            model = PayoutStatement(
                tenant_id=tenant_id,
                provider=payload.provider,
                external_tx_ref=row.external_tx_ref,
                payout_currency=row.payout_currency.upper(),
                payout_amount=payout_amount,
                settled_base_currency=row.settled_base_currency.upper(),
                settled_base_amount=settled_amount,
                actual_fx_rate=actual,
                expected_fx_rate=expected,
                fx_variance_base=variance,
                match_status="unmatched",
                paid_at=row.paid_at or now,
                created_at=now,
            )
            session.add(model)
            imported.append(model)
            known.add((payload.provider, row.external_tx_ref))
        await session.flush()

        matched = 0
        for model in imported:
            hint = next(
                (
                    row.reference_hint
                    for row in rows
                    if row.external_tx_ref == model.external_tx_ref and row.reference_hint
                ),
                None,
            )
            if hint and await self._match_by_reference(session, tenant_id, model, hint, principal):
                matched += 1

        for model in imported:
            await record_audit_event(
                session,
                tenant_id=tenant_id,
                actor_type="human_user",
                actor_id=str(principal.user_id),
                action="finance.payout_statement.imported",
                resource_type="payout_statement",
                resource_id=model.id,
                after_state={
                    "provider": model.provider,
                    "external_tx_ref": model.external_tx_ref,
                    "payout_currency": model.payout_currency,
                    "payout_amount": _money(model.payout_amount),
                    "settled_base_amount": _money(model.settled_base_amount),
                    "actual_fx_rate": _rate(model.actual_fx_rate),
                    "expected_fx_rate": _rate(model.expected_fx_rate),
                    "fx_variance_base": _money(model.fx_variance_base),
                },
            )
        await session.flush()
        return PayoutImportResultRead(
            imported_count=len(imported),
            skipped_count=skipped,
            matched_count=matched,
            items=[self._payout_to_read(model) for model in imported],
        )

    @staticmethod
    def _parse_payout_csv(csv_text: str) -> list[PayoutStatementInput]:
        raw_rows = _csv_rows(csv_text, PAYOUT_COLUMNS, PAYOUT_OPTIONAL_COLUMNS)
        parsed: list[PayoutStatementInput] = []
        for row in raw_rows:
            parsed.append(
                PayoutStatementInput.model_validate(
                    {
                        "external_tx_ref": row["external_tx_ref"],
                        "payout_currency": row["payout_currency"],
                        "payout_amount": _row_decimal(row, "payout_amount"),
                        "settled_base_currency": row["settled_base_currency"],
                        "settled_base_amount": _row_decimal(row, "settled_base_amount"),
                        "expected_fx_rate": _row_decimal(row, "expected_fx_rate"),
                        "actual_fx_rate": (
                            _row_decimal(row, "actual_fx_rate")
                            if row.get("actual_fx_rate")
                            else None
                        ),
                        "paid_at": row.get("paid_at") or None,
                        "reference_hint": row.get("reference_hint") or None,
                    }
                )
            )
        return parsed

    @staticmethod
    def _payout_to_read(model: PayoutStatement) -> PayoutStatementRead:
        return PayoutStatementRead(
            id=model.id,
            tenant_id=model.tenant_id,
            provider=model.provider,
            external_tx_ref=model.external_tx_ref,
            payout_currency=model.payout_currency,
            payout_amount=_q2(model.payout_amount),
            settled_base_currency=model.settled_base_currency,
            settled_base_amount=_q2(model.settled_base_amount),
            actual_fx_rate=_q6(model.actual_fx_rate),
            expected_fx_rate=_q6(model.expected_fx_rate),
            fx_variance_base=_q2(model.fx_variance_base),
            matched_settlement_batch_id=model.matched_settlement_batch_id,
            matched_order_id=model.matched_order_id,
            match_status=model.match_status,
            paid_at=model.paid_at,
            created_at=model.created_at,
        )

    async def list_payouts(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        provider: str | None = None,
        match_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> PayoutStatementListRead:
        clauses = ["tenant_id = :tid"]
        filter_params: dict[str, object] = {"tid": tenant_id}
        if provider:
            clauses.append("provider = :provider")
            filter_params["provider"] = provider
        if match_status:
            clauses.append("match_status = :match_status")
            filter_params["match_status"] = match_status
        where_sql = " AND ".join(clauses)
        rows = (
            (
                await session.execute(
                    select(PayoutStatement)
                    .where(text(where_sql))
                    .order_by(PayoutStatement.paid_at.desc(), PayoutStatement.created_at.desc())
                    .limit(limit)
                    .offset(offset),
                    filter_params,
                )
            )
            .scalars()
            .all()
        )
        items = [self._payout_to_read(row) for row in rows]
        totals = (
            await session.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total_count,
                           COUNT(*) FILTER (WHERE match_status = 'matched') AS matched_count,
                           COUNT(*) FILTER (WHERE match_status = 'unmatched') AS unmatched_count,
                           COALESCE(SUM(fx_variance_base), 0) AS fx_variance_base
                    FROM payout_statements WHERE {where_sql}
                    """
                ),
                filter_params,
            )
        ).one()
        return PayoutStatementListRead(
            items=items,
            total_count=int(totals.total_count or 0),
            matched_count=int(totals.matched_count or 0),
            unmatched_count=int(totals.unmatched_count or 0),
            total_fx_variance_base=_q2(_as_decimal(totals.fx_variance_base, "fx_variance_base")),
        )

    async def _match_by_reference(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payout: PayoutStatement,
        reference: str,
        principal: CurrentPrincipal,
    ) -> bool:
        """Exact `reference_hint` -> `batch_ref`, else -> `order_no` / `external_order_id`."""
        needle = reference.strip()
        if not needle:
            return False
        batch = (
            await session.execute(
                select(SettlementBatch).where(
                    SettlementBatch.tenant_id == tenant_id,
                    SettlementBatch.batch_ref == needle,
                )
            )
        ).scalar_one_or_none()
        if batch is not None:
            await self._link_batch(session, tenant_id, payout, batch, principal, needle)
            return True
        order_id = await session.scalar(
            text(
                "SELECT id FROM orders WHERE tenant_id = :tid "
                "AND (order_no = :ref OR external_order_id = :ref) ORDER BY created_at LIMIT 1"
            ),
            {"tid": tenant_id, "ref": needle},
        )
        if order_id is not None:
            await self._link_order(session, tenant_id, payout, order_id, principal, needle)
            return True
        return False

    async def _link_batch(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payout: PayoutStatement,
        batch: SettlementBatch,
        principal: CurrentPrincipal,
        reason: str,
    ) -> None:
        before = {"match_status": payout.match_status, "batch_status": batch.status}
        payout.matched_settlement_batch_id = batch.id
        payout.matched_order_id = None
        payout.match_status = "matched"
        batch.status = "reconciled"
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="finance.payout_statement.matched",
            resource_type="payout_statement",
            resource_id=payout.id,
            before_state=before,
            after_state={
                "match_status": "matched",
                "matched_settlement_batch_id": str(batch.id),
                "batch_ref": batch.batch_ref,
                "reason": reason,
            },
        )

    async def _link_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payout: PayoutStatement,
        order_id: UUID,
        principal: CurrentPrincipal,
        reason: str,
    ) -> None:
        before = {"match_status": payout.match_status}
        payout.matched_order_id = order_id
        payout.matched_settlement_batch_id = None
        payout.match_status = "matched"
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="finance.payout_statement.matched",
            resource_type="payout_statement",
            resource_id=payout.id,
            before_state=before,
            after_state={
                "match_status": "matched",
                "matched_order_id": str(order_id),
                "reason": reason,
            },
        )

    async def auto_match_payouts(
        self, session: AsyncSession, tenant_id: UUID, principal: CurrentPrincipal
    ) -> PayoutAutoMatchResultRead:
        """Match every unmatched payout by exact reference, then currency + closest amount."""
        self._assert_writer(principal, tenant_id, "Reconcile Payouts")
        payouts = (
            (
                await session.execute(
                    select(PayoutStatement)
                    .where(
                        PayoutStatement.tenant_id == tenant_id,
                        PayoutStatement.match_status == "unmatched",
                    )
                    .order_by(PayoutStatement.paid_at, PayoutStatement.created_at)
                )
            )
            .scalars()
            .all()
        )

        matched_ids: list[UUID] = []
        for payout in payouts:
            if await self._match_by_reference(
                session, tenant_id, payout, payout.external_tx_ref, principal
            ):
                matched_ids.append(payout.id)
                continue

            batch = await self._closest_batch(session, tenant_id, payout)
            if batch is not None:
                await self._link_batch(
                    session,
                    tenant_id,
                    payout,
                    batch,
                    principal,
                    f"amount+currency within {MATCH_TOLERANCE} of batch_ref={batch.batch_ref}",
                )
                matched_ids.append(payout.id)
                continue

            order_id = await self._closest_order(session, tenant_id, payout)
            if order_id is not None:
                await self._link_order(
                    session,
                    tenant_id,
                    payout,
                    order_id,
                    principal,
                    f"amount+currency within {MATCH_TOLERANCE} of a paid order",
                )
                matched_ids.append(payout.id)

        remaining = int(
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM payout_statements "
                    "WHERE tenant_id = :tid AND match_status = 'unmatched'"
                ),
                {"tid": tenant_id},
            )
            or 0
        )
        variance_raw = await session.scalar(
            text(
                "SELECT COALESCE(SUM(fx_variance_base), 0) FROM payout_statements "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        refreshed = (
            (
                await session.execute(
                    select(PayoutStatement)
                    .where(
                        PayoutStatement.tenant_id == tenant_id,
                        PayoutStatement.id.in_(matched_ids),
                    )
                    .order_by(PayoutStatement.paid_at, PayoutStatement.created_at)
                )
            )
            .scalars()
            .all()
            if matched_ids
            else []
        )
        return PayoutAutoMatchResultRead(
            matched_count=len(matched_ids),
            remaining_unmatched_count=remaining,
            total_fx_variance_base=_q2(
                _as_decimal(variance_raw if variance_raw is not None else 0, "fx_variance_base")
            ),
            items=[self._payout_to_read(model) for model in refreshed],
        )

    async def _closest_batch(
        self, session: AsyncSession, tenant_id: UUID, payout: PayoutStatement
    ) -> SettlementBatch | None:
        candidates = (
            (
                await session.execute(
                    select(SettlementBatch).where(
                        SettlementBatch.tenant_id == tenant_id,
                        SettlementBatch.status == "imported",
                        SettlementBatch.currency == payout.payout_currency,
                    )
                )
            )
            .scalars()
            .all()
        )
        best: SettlementBatch | None = None
        best_gap: Decimal | None = None
        amount = _q2(payout.payout_amount)
        for batch in candidates:
            gap = abs(_q2(batch.net_payout_ccy) - amount)
            if gap > MATCH_TOLERANCE:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = batch, gap
        return best

    async def _closest_order(
        self, session: AsyncSession, tenant_id: UUID, payout: PayoutStatement
    ) -> UUID | None:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT o.id, o.total_amount
                    FROM orders o
                    WHERE o.tenant_id = :tid
                      AND o.currency = :ccy
                      AND o.payment_status = 'paid'
                      AND o.order_mode IN ('b2b_sample', 'b2b_wholesale')
                      AND NOT EXISTS (
                          SELECT 1 FROM payout_statements p
                          WHERE p.tenant_id = o.tenant_id AND p.matched_order_id = o.id
                            AND p.match_status = 'matched'
                      )
                    ORDER BY o.created_at
                    """
                ),
                {"tid": tenant_id, "ccy": payout.payout_currency},
            )
        ).all()
        best_id: UUID | None = None
        best_gap: Decimal | None = None
        amount = _q2(payout.payout_amount)
        for row in rows:
            gap = abs(_q2(_as_decimal(row.total_amount, "total_amount")) - amount)
            if gap > MATCH_TOLERANCE:
                continue
            if best_gap is None or gap < best_gap:
                best_id, best_gap = row.id, gap
        return best_id

    # ---------------------------------------------------------- voucher export
    async def export_journal_csv(
        self, session: AsyncSession, tenant_id: UUID, export_format: str
    ) -> str:
        """Balanced double-entry voucher export across the four accounting dialects."""
        if export_format not in JOURNAL_FORMATS:
            raise AppError(
                slug="unsupported-journal-format",
                status_code=422,
                code="ERR_UNSUPPORTED_JOURNAL_FORMAT",
                title="Unsupported Journal Format",
                detail=f"format must be one of {', '.join(JOURNAL_FORMATS)}.",
            )
        entries = await self._build_journal_entries(session, tenant_id)
        for entry in entries:
            if entry.debit < Decimal("0.00") or entry.credit < Decimal("0.00"):
                raise AppError(
                    slug="invalid-journal-entry",
                    status_code=500,
                    code="ERR_INVALID_JOURNAL_ENTRY",
                    title="Invalid Journal Entry",
                    detail=f"Voucher {entry.voucher_no} carries a negative amount.",
                )
        total_debit = sum((entry.debit for entry in entries), Decimal("0.00"))
        total_credit = sum((entry.credit for entry in entries), Decimal("0.00"))
        if _q2(total_debit) != _q2(total_credit):
            raise AppError(
                slug="unbalanced-journal",
                status_code=500,
                code="ERR_UNBALANCED_JOURNAL",
                title="Unbalanced Journal Export",
                detail=(
                    "Refusing to export an unbalanced voucher file: "
                    f"debits={_money(total_debit)} credits={_money(total_credit)}."
                ),
            )
        return self._render_journal(entries, export_format)

    async def _build_journal_entries(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[JournalEntry]:
        batches = (
            (
                await session.execute(
                    select(SettlementBatch)
                    .where(
                        SettlementBatch.tenant_id == tenant_id,
                        SettlementBatch.status == "reconciled",
                    )
                    .order_by(SettlementBatch.period_end, SettlementBatch.batch_ref)
                )
            )
            .scalars()
            .all()
        )
        entries: list[JournalEntry] = []
        for batch in batches:
            entries.extend(await self._batch_entries(session, tenant_id, batch))

        # B2B orders that were matched to a payout but never appear in a
        # reconciled marketplace batch receive the cash-receipt voucher only.
        order_payouts = (
            (
                await session.execute(
                    select(PayoutStatement)
                    .where(
                        PayoutStatement.tenant_id == tenant_id,
                        PayoutStatement.match_status == "matched",
                        PayoutStatement.matched_order_id.is_not(None),
                    )
                    .order_by(PayoutStatement.paid_at)
                )
            )
            .scalars()
            .all()
        )
        for payout in order_payouts:
            entries.extend(self._payout_receipt_entries(payout, f"PAYOUT-{payout.external_tx_ref}"))
        return entries

    async def _batch_entries(
        self, session: AsyncSession, tenant_id: UUID, batch: SettlementBatch
    ) -> list[JournalEntry]:
        fx = _q6(_as_decimal(batch.fx_rate_to_base, "fx_rate_to_base"))
        gross = _q2(_as_decimal(batch.gross_sales_ccy, "gross_sales_ccy") * fx)
        commission = _q2(_as_decimal(batch.commission_ccy, "commission_ccy") * fx)
        fulfillment = _q2(_as_decimal(batch.fulfillment_fee_ccy, "fulfillment_fee_ccy") * fx)
        storage = _q2(_as_decimal(batch.storage_fee_ccy, "storage_fee_ccy") * fx)
        ad = _q2(_as_decimal(batch.ad_fee_ccy, "ad_fee_ccy") * fx)
        refund = _q2(_as_decimal(batch.refund_ccy, "refund_ccy") * fx)
        net_payout = _q2(_as_decimal(batch.net_payout_ccy, "net_payout_ccy") * fx)

        cogs_raw = await session.scalar(
            text(
                """
                SELECT COALESCE(SUM(s.cogs_base), 0)
                FROM (
                    SELECT DISTINCT l.order_id
                    FROM settlement_lines l
                    WHERE l.tenant_id = :tid AND l.batch_id = :bid
                      AND l.order_id IS NOT NULL
                ) linked
                JOIN order_profit_snapshots s
                  ON s.order_id = linked.order_id AND s.tenant_id = :tid
                """
            ),
            {"tid": tenant_id, "bid": batch.id},
        )
        cogs = _q2(_as_decimal(cogs_raw if cogs_raw is not None else 0, "cogs_base"))

        payout_fx_raw = await session.scalar(
            text(
                "SELECT COALESCE(SUM(fx_variance_base), 0) FROM payout_statements "
                "WHERE tenant_id = :tid AND matched_settlement_batch_id = :bid"
            ),
            {"tid": tenant_id, "bid": batch.id},
        )
        payout_fx = _q2(_as_decimal(payout_fx_raw if payout_fx_raw is not None else 0, "payout_fx"))

        voucher = f"STL-{batch.batch_ref}"
        entry_date = batch.period_end
        currency = batch.currency
        label = f"{batch.channel_type} settlement {batch.batch_ref}"
        out: list[JournalEntry] = [
            _entry(voucher, entry_date, currency, ACC_CLEARING, f"{label} gross", debit=gross),
            _entry(voucher, entry_date, currency, ACC_REVENUE, label, credit=gross),
        ]
        fee_buckets: tuple[tuple[tuple[str, str], Decimal, str], ...] = (
            (ACC_COMMISSION, commission, "commission"),
            (ACC_FULFILLMENT, fulfillment + storage, "fulfillment & storage"),
            (ACC_ADVERTISING, ad, "advertising"),
            (ACC_RETURNS, refund, "refund"),
        )
        for account, amount, what in fee_buckets:
            if amount == 0:
                continue
            out.append(
                _entry(voucher, entry_date, currency, account, f"{label} {what}", debit=amount)
            )
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_CLEARING,
                    f"{label} {what}",
                    credit=amount,
                )
            )
        if cogs != 0:
            out.append(_entry(voucher, entry_date, currency, ACC_COGS, f"{label} COGS", debit=cogs))
            out.append(
                _entry(voucher, entry_date, currency, ACC_INVENTORY, f"{label} COGS", credit=cogs)
            )
        if net_payout != 0:
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_CASH,
                    f"{label} net payout",
                    debit=net_payout,
                )
            )
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_CLEARING,
                    f"{label} net payout",
                    credit=net_payout,
                )
            )

        # Whatever cent-level difference the statement itself carries is a
        # clearing rounding/FX adjustment, booked so the voucher stays balanced.
        residual = _q2(gross - (commission + fulfillment + storage + ad + refund + net_payout))
        if residual > 0:
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_FX,
                    f"{label} clearing residual",
                    debit=residual,
                )
            )
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_CLEARING,
                    f"{label} clearing residual",
                    credit=residual,
                )
            )
        elif residual < 0:
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_CLEARING,
                    f"{label} clearing residual",
                    debit=-residual,
                )
            )
            out.append(
                _entry(
                    voucher,
                    entry_date,
                    currency,
                    ACC_FX,
                    f"{label} clearing residual",
                    credit=-residual,
                )
            )

        if payout_fx != 0:
            out.extend(
                self._fx_entries(
                    voucher,
                    entry_date,
                    currency,
                    payout_fx,
                    f"{label} realised FX",
                )
            )
        return out

    @staticmethod
    def _fx_entries(
        voucher: str, entry_date: date, currency: str, variance: Decimal, label: str
    ) -> list[JournalEntry]:
        """FX gain credits `FX Gain / Loss`, an FX loss debits it, cash is the counterparty."""
        if variance > 0:
            return [
                _entry(voucher, entry_date, currency, ACC_CASH, label, debit=variance),
                _entry(voucher, entry_date, currency, ACC_FX, label, credit=variance),
            ]
        loss = -variance
        return [
            _entry(voucher, entry_date, currency, ACC_FX, label, debit=loss),
            _entry(voucher, entry_date, currency, ACC_CASH, label, credit=loss),
        ]

    @classmethod
    def _payout_receipt_entries(cls, payout: PayoutStatement, voucher: str) -> list[JournalEntry]:
        expected_rate = _q6(_as_decimal(payout.expected_fx_rate, "expected_fx_rate"))
        expected_base = _q2(_q2(payout.payout_amount) * expected_rate)
        settled = _q2(payout.settled_base_amount)
        variance = _q2(settled - expected_base)
        entry_date = payout.paid_at.date()
        currency = payout.payout_currency
        label = f"{payout.provider} payout {payout.external_tx_ref}"
        out = [
            _entry(voucher, entry_date, currency, ACC_CASH, label, debit=settled),
            _entry(voucher, entry_date, currency, ACC_AR, label, credit=expected_base),
        ]
        if variance != 0:
            out.extend(cls._fx_entries(voucher, entry_date, currency, variance, label))
        return out

    @staticmethod
    def _render_journal(entries: Sequence[JournalEntry], export_format: str) -> str:
        header: tuple[str, ...]
        rows: list[tuple[str, ...]]
        if export_format == "quickbooks":
            header = (
                "Journal No",
                "Journal Date",
                "Account Code",
                "Account Name",
                "Debit",
                "Credit",
                "Memo",
                "Currency",
            )
            rows = [
                (
                    entry.voucher_no,
                    entry.entry_date.isoformat(),
                    entry.account_code,
                    entry.account_name,
                    _money(entry.debit),
                    _money(entry.credit),
                    entry.description,
                    entry.currency,
                )
                for entry in entries
            ]
        elif export_format == "xero":
            header = (
                "Narration",
                "Date",
                "Account Code",
                "Account Name",
                "Debit",
                "Credit",
                "Currency",
            )
            rows = [
                (
                    entry.description,
                    entry.entry_date.isoformat(),
                    entry.account_code,
                    entry.account_name,
                    _money(entry.debit),
                    _money(entry.credit),
                    entry.currency,
                )
                for entry in entries
            ]
        elif export_format == "kingdee":
            header = (
                "凭证字号",
                "日期",
                "科目代码",
                "科目名称",
                "摘要",
                "借方本位币",
                "贷方本位币",
                "币别",
            )
            rows = [
                (
                    entry.voucher_no,
                    entry.entry_date.isoformat(),
                    entry.account_code,
                    entry.account_name,
                    entry.description,
                    _money(entry.debit),
                    _money(entry.credit),
                    entry.currency,
                )
                for entry in entries
            ]
        else:
            header = (
                "voucher_no",
                "entry_date",
                "currency",
                "account_code",
                "account_name",
                "description",
                "debit_base",
                "credit_base",
            )
            rows = [
                (
                    entry.voucher_no,
                    entry.entry_date.isoformat(),
                    entry.currency,
                    entry.account_code,
                    entry.account_name,
                    entry.description,
                    _money(entry.debit),
                    _money(entry.credit),
                )
                for entry in entries
            ]
        return _write_csv(header, rows)
