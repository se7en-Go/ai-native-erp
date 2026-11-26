"""B2B buyer magic-link self-service portal.

A buyer receives one opaque link (`/portal/erp_b2b_<urlsafe32>`), and that link alone
authenticates a *narrow* read/accept/upload surface against a single quote:

* the seller letterhead + bank wire block comes from `tenants.ai_policy_settings
  -> 'pi_template'` and never from raw tenant settings (no secrets/API keys);
* line items expose buyer-facing prices only — `unit_cost_base_snapshot`, landed cost,
  margins and internal notes are never selected;
* accepting a quote stamps `b2b_portal_tokens` and writes a `webhook` audit event;
* a T/T slip upload always lands as an **unverified** `payment_records` row
  (`verified_by_user_id = NULL`), so the L0 human Finance gate still decides the AR
  settlement and the order stays `unpaid` until Finance verifies.

Plaintext tokens are never persisted: only `token_prefix` + the SHA-256 hex
`token_hash`, resolved pre-tenant through the `SECURITY DEFINER`
`resolve_b2b_portal_token(TEXT)` helper created by migration `0021`.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.crm.models import Quote, QuoteItem
from erp_core.crm.quote_service import QuoteService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import Incoterm, QuoteStatus
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.schemas import parse_pi_template

PORTAL_TOKEN_PREFIX = "erp_b2b_"
PORTAL_PATH_PREFIX = "/portal/"

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
ALLOWED_RECEIPT_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/webp"}
)
# 17TRACK events carry carrier narrative only; anything else (internal cost, notes)
# is dropped at the response edge so the public payload can never leak internals.
_TRACKING_EVENT_KEYS: tuple[str, ...] = (
    "description",
    "location",
    "stage",
    "status",
    "time_iso",
    "time_utc",
)

DocumentType = Literal["pi", "ci", "pl"]


def hash_portal_token(raw_token: str) -> str:
    """Return the lowercase SHA-256 hex digest of a plain buyer portal token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _cent(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _invalid_token_error(
    detail: str = "This buyer portal link is invalid or has been revoked.",
) -> AppError:
    return AppError(
        slug="invalid-portal-token",
        title="Invalid Portal Link",
        status_code=401,
        detail=detail,
        code="ERR_INVALID_PORTAL_TOKEN",
    )


def _quote_not_found_error(quote_id: UUID) -> AppError:
    return AppError(
        slug="quote-not-found",
        status_code=404,
        code="ERR_QUOTE_NOT_FOUND",
        title="Quote Not Found",
        detail=f"Quote '{quote_id}' was not found.",
    )


def _duplicate_ref_error(ref: str) -> AppError:
    return AppError(
        slug="duplicate-payment-ref",
        status_code=409,
        code="ERR_DUPLICATE_PAYMENT_REF",
        title="Duplicate Payment Reference",
        detail=(
            f"Payment reference '{ref}' has already been recorded for this workspace; "
            "a bank transaction can only be booked once."
        ),
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PortalLinkCreateRequest(BaseModel):
    buyer_email: EmailStr | None = None
    allow_accept: bool = True
    allow_receipt_upload: bool = True
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class PortalLinkRead(BaseModel):
    """Operator-facing token metadata. Never carries the plaintext token or its hash."""

    id: UUID
    quote_id: UUID
    token_prefix: str
    buyer_email: str | None = None
    allow_accept: bool
    allow_receipt_upload: bool
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_accessed_at: datetime | None = None
    accepted_at: datetime | None = None
    accepted_by_name: str | None = None
    is_active: bool
    created_at: datetime


class PortalLinkCreateResponse(BaseModel):
    link: PortalLinkRead
    plain_token: str
    portal_path: str


class ResolvedPortalToken(BaseModel):
    token_id: UUID
    tenant_id: UUID
    quote_id: UUID
    buyer_email: str | None = None
    allow_accept: bool
    allow_receipt_upload: bool
    expires_at: datetime | None = None
    accepted_at: datetime | None = None
    accepted_by_name: str | None = None


class PortalSellerRead(BaseModel):
    company_name: str
    address: str
    phone: str
    email: str
    website: str
    tax_or_reg_no: str
    bank_beneficiary_name: str
    bank_name: str
    bank_account_no: str
    bank_swift_code: str
    bank_address: str
    terms_notes: str


class PortalQuoteLineRead(BaseModel):
    sku: str
    description: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class PortalQuoteRead(BaseModel):
    quote_id: UUID
    quote_no: str
    version: int
    status: QuoteStatus
    incoterm: Incoterm
    port_of_loading: str | None = None
    port_of_destination: str | None = None
    payment_terms: str
    quote_currency: str
    fx_rate_snapshot: Decimal
    fx_locked_until: datetime
    freight_fee: Decimal
    insurance_fee: Decimal
    customs_duty_estimate: Decimal
    total_amount: Decimal
    items: list[PortalQuoteLineRead]


class PortalReceivableRead(BaseModel):
    id: UUID
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    status: str
    due_date: date | None = None


class PortalTrackingEventRead(BaseModel):
    occurred_at: datetime | None = None
    description: str
    location: str | None = None
    stage: str | None = None


class PortalShipmentRead(BaseModel):
    id: UUID
    carrier: str | None = None
    tracking_number: str
    standardized_status: str
    sub_status: str | None = None
    last_event_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    tracking_events: list[PortalTrackingEventRead] = Field(default_factory=list)


class PortalOrderRead(BaseModel):
    order_id: UUID
    order_no: str
    fulfillment_status: str
    payment_status: str
    total_amount: Decimal
    currency: str
    receivables: list[PortalReceivableRead] = Field(default_factory=list)
    shipments: list[PortalShipmentRead] = Field(default_factory=list)


class BuyerPortalViewRead(BaseModel):
    seller: PortalSellerRead
    quote: PortalQuoteRead
    order: PortalOrderRead | None = None
    can_accept: bool
    can_upload_receipt: bool
    accepted_at: datetime | None = None
    accepted_by_name: str | None = None


class PortalAcceptRequest(BaseModel):
    buyer_name: str = Field(min_length=1, max_length=120)


class PortalAcceptResponse(BaseModel):
    quote_id: UUID
    quote_no: str
    status: QuoteStatus
    accepted_at: datetime | None = None
    accepted_by_name: str | None = None
    order_id: UUID | None = None


class PortalReceiptRequest(BaseModel):
    """T/T bank slip payload. Accepted as multipart form fields or a JSON body."""

    rp_id: UUID
    payment_method: str = Field(default="bank_transfer", min_length=1, max_length=64)
    external_tx_ref: str | None = Field(default=None, max_length=120)
    amount_received: Decimal = Field(gt=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)


class PortalReceiptResponse(BaseModel):
    payment_id: UUID
    rp_id: UUID
    order_id: UUID
    amount_received: Decimal
    currency: str
    receipt_r2_key: str | None = None
    status: Literal["pending_verification"] = "pending_verification"


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class ReceiptStoragePort(Protocol):
    async def upload_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str: ...


class PortalPaymentPort(Protocol):
    async def create_unverified_payment(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        rp_id: UUID,
        payment_method: str,
        external_tx_ref: str | None,
        amount_received: Decimal,
        currency: str,
        actual_fx_rate: Decimal,
        receipt_r2_key: str | None,
        paid_at: datetime,
    ) -> UUID: ...


class _RuntimePortalPaymentAdapter:
    """Lazy bridge into the finance table.

    `erp_core.crm` must never import another domain's ORM models (enforced by
    `tests/test_infra_smoke.py::test_no_cross_module_orm_model_imports`), so the
    unverified receipt row is written with a parameterized, RLS-scoped INSERT that
    mirrors `PaymentRecord` exactly: `verified_by_user_id` stays NULL.
    """

    async def create_unverified_payment(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        rp_id: UUID,
        payment_method: str,
        external_tx_ref: str | None,
        amount_received: Decimal,
        currency: str,
        actual_fx_rate: Decimal,
        receipt_r2_key: str | None,
        paid_at: datetime,
    ) -> UUID:
        payment_id = uuid4()
        try:
            inserted = await session.scalar(
                text(
                    """
                    INSERT INTO payment_records (
                        id, tenant_id, rp_id, payment_method, external_tx_ref,
                        amount_received, currency, actual_fx_rate, gateway_fee,
                        receipt_r2_key, verified_by_user_id, paid_at
                    ) VALUES (
                        :id, :tid, :rp, :method, :ref,
                        :amount, :ccy, :fx, 0.00,
                        :receipt_key, NULL, :paid_at
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": payment_id,
                    "tid": tenant_id,
                    "rp": rp_id,
                    "method": payment_method,
                    "ref": external_tx_ref,
                    "amount": amount_received,
                    "ccy": currency,
                    "fx": actual_fx_rate,
                    "receipt_key": receipt_r2_key,
                    "paid_at": paid_at,
                },
            )
        except IntegrityError:
            if external_tx_ref:
                raise _duplicate_ref_error(external_tx_ref) from None
            raise
        return UUID(str(inserted))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_link(row: Any) -> PortalLinkRead:
    expires_at = row["expires_at"]
    revoked_at = row["revoked_at"]
    is_active = bool(
        revoked_at is None
        and (expires_at is None or expires_at > datetime.now(UTC))
    )
    return PortalLinkRead(
        id=row["id"],
        quote_id=row["quote_id"],
        token_prefix=str(row["token_prefix"]),
        buyer_email=row["buyer_email"],
        allow_accept=bool(row["allow_accept"]),
        allow_receipt_upload=bool(row["allow_receipt_upload"]),
        expires_at=expires_at,
        revoked_at=revoked_at,
        last_accessed_at=row["last_accessed_at"],
        accepted_at=row["accepted_at"],
        accepted_by_name=row["accepted_by_name"],
        is_active=is_active,
        created_at=row["created_at"],
    )


_LINK_COLUMNS = (
    "id, quote_id, token_prefix, buyer_email, allow_accept, allow_receipt_upload, "
    "expires_at, revoked_at, last_accessed_at, accepted_at, accepted_by_name, created_at"
)


def _safe_filename(raw: str | None, content_type: str) -> str:
    candidate = (raw or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch in "-_.").strip("._")
    if not cleaned:
        cleaned = {
            "application/pdf": "receipt.pdf",
            "image/png": "receipt.png",
            "image/jpeg": "receipt.jpg",
            "image/webp": "receipt.webp",
        }.get(content_type, "receipt")
    return cleaned[:120]


def _parse_event_time(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _sanitize_tracking_events(raw_events: Any) -> list[PortalTrackingEventRead]:
    if not isinstance(raw_events, list):
        return []
    sanitized: list[PortalTrackingEventRead] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        picked = {key: event.get(key) for key in _TRACKING_EVENT_KEYS}
        description = str(picked.get("description") or "").strip()
        location = picked.get("location")
        stage = picked.get("stage") or picked.get("status")
        sanitized.append(
            PortalTrackingEventRead(
                occurred_at=_parse_event_time(
                    picked.get("time_utc") or picked.get("time_iso")
                ),
                description=description or "Tracking update",
                location=str(location) if location else None,
                stage=str(stage) if stage else None,
            )
        )
    return sanitized


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PortalService:
    """Operator link lifecycle plus the buyer-facing self-service read/write surface."""

    def __init__(
        self,
        quote_service: QuoteService | None = None,
        payment_port: PortalPaymentPort | None = None,
    ) -> None:
        self._quotes = quote_service or QuoteService()
        self._payments: PortalPaymentPort = payment_port or _RuntimePortalPaymentAdapter()

    # -- operator ----------------------------------------------------------

    async def create_portal_link(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        quote_id: UUID,
        req: PortalLinkCreateRequest,
    ) -> PortalLinkCreateResponse:
        quote = await session.scalar(
            select(Quote).where(Quote.tenant_id == principal.tenant_id, Quote.id == quote_id)
        )
        if quote is None:
            raise _quote_not_found_error(quote_id)

        token_id = uuid4()
        plain_token = f"{PORTAL_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        token_prefix = plain_token[:16]
        token_hash = hash_portal_token(plain_token)
        expires_at = (
            datetime.now(UTC) + timedelta(days=req.expires_in_days)
            if req.expires_in_days is not None
            else None
        )
        buyer_email = str(req.buyer_email) if req.buyer_email else None
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO b2b_portal_tokens (
                        id, tenant_id, quote_id, created_by_user_id,
                        token_prefix, token_hash, buyer_email,
                        allow_accept, allow_receipt_upload, expires_at
                    ) VALUES (
                        :id, :tid, :qid, :uid,
                        :prefix, :thash, :email,
                        :allow_accept, :allow_receipt, :expires_at
                    )
                    RETURNING """
                    + _LINK_COLUMNS
                ),
                {
                    "id": token_id,
                    "tid": principal.tenant_id,
                    "qid": quote_id,
                    "uid": principal.user_id,
                    "prefix": token_prefix,
                    "thash": token_hash,
                    "email": buyer_email,
                    "allow_accept": req.allow_accept,
                    "allow_receipt": req.allow_receipt_upload,
                    "expires_at": expires_at,
                },
            )
        ).one()
        await record_audit_event(
            session,
            tenant_id=principal.tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="b2b.portal_link.created",
            resource_type="b2b_portal_token",
            resource_id=token_id,
            after_state={
                "quote_id": str(quote_id),
                "quote_no": quote.quote_no,
                "token_prefix": token_prefix,
                "buyer_email": buyer_email,
                "allow_accept": req.allow_accept,
                "allow_receipt_upload": req.allow_receipt_upload,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return PortalLinkCreateResponse(
            link=_row_to_link(row._mapping),
            plain_token=plain_token,
            portal_path=f"{PORTAL_PATH_PREFIX}{plain_token}",
        )

    async def list_portal_links(
        self, session: AsyncSession, tenant_id: UUID, quote_id: UUID
    ) -> list[PortalLinkRead]:
        rows = (
            await session.execute(
                text(
                    "SELECT " + _LINK_COLUMNS + " FROM b2b_portal_tokens "
                    "WHERE tenant_id = :tid AND quote_id = :qid "
                    "ORDER BY created_at DESC"
                ),
                {"tid": tenant_id, "qid": quote_id},
            )
        ).mappings().all()
        return [_row_to_link(row) for row in rows]

    async def revoke_portal_link(
        self, session: AsyncSession, tenant_id: UUID, token_id: UUID, actor_id: str
    ) -> PortalLinkRead:
        row = (
            await session.execute(
                text(
                    "UPDATE b2b_portal_tokens SET revoked_at = COALESCE(revoked_at, now()) "
                    "WHERE id = :id AND tenant_id = :tid RETURNING " + _LINK_COLUMNS
                ),
                {"id": token_id, "tid": tenant_id},
            )
        ).first()
        if row is None:
            raise AppError(
                slug="portal-link-not-found",
                title="Portal Link Not Found",
                status_code=404,
                detail=f"Portal link {token_id} was not found.",
                code="ERR_PORTAL_LINK_NOT_FOUND",
            )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="b2b.portal_link.revoked",
            resource_type="b2b_portal_token",
            resource_id=token_id,
            after_state={"revoked": True},
        )
        return _row_to_link(row._mapping)

    # -- token resolution ---------------------------------------------------

    async def resolve_token(
        self, session_factory: async_sessionmaker[AsyncSession], raw_token: str
    ) -> ResolvedPortalToken:
        """Resolve a plaintext link pre-tenant, then stamp `last_accessed_at` under RLS."""
        candidate = (raw_token or "").strip()
        if not candidate.startswith(PORTAL_TOKEN_PREFIX):
            raise _invalid_token_error()
        token_hash = hash_portal_token(candidate)
        async with system_session(session_factory) as sys_session:
            row = (
                await sys_session.execute(
                    text(
                        """
                        SELECT token_id, tenant_id, quote_id, buyer_email,
                               allow_accept, allow_receipt_upload,
                               expires_at, revoked_at, accepted_at, accepted_by_name
                        FROM resolve_b2b_portal_token(:token_hash)
                        """
                    ),
                    {"token_hash": token_hash},
                )
            ).first()
        if row is None:
            raise _invalid_token_error()
        if row.revoked_at is not None:
            raise _invalid_token_error("This buyer portal link has been revoked.")
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            raise _invalid_token_error("This buyer portal link has expired.")

        async with tenant_session(session_factory, row.tenant_id) as t_session:
            await t_session.execute(
                text(
                    "UPDATE b2b_portal_tokens SET last_accessed_at = now() "
                    "WHERE id = :id AND tenant_id = :tid"
                ),
                {"id": row.token_id, "tid": row.tenant_id},
            )
        return ResolvedPortalToken(
            token_id=row.token_id,
            tenant_id=row.tenant_id,
            quote_id=row.quote_id,
            buyer_email=row.buyer_email,
            allow_accept=bool(row.allow_accept),
            allow_receipt_upload=bool(row.allow_receipt_upload),
            expires_at=row.expires_at,
            accepted_at=row.accepted_at,
            accepted_by_name=row.accepted_by_name,
        )

    # -- buyer: read --------------------------------------------------------

    async def _load_quote(self, session: AsyncSession, resolved: ResolvedPortalToken) -> Quote:
        quote = await session.scalar(
            select(Quote).where(
                Quote.tenant_id == resolved.tenant_id, Quote.id == resolved.quote_id
            )
        )
        if quote is None:
            raise _quote_not_found_error(resolved.quote_id)
        return quote

    async def get_portal_view(
        self, session: AsyncSession, resolved: ResolvedPortalToken
    ) -> BuyerPortalViewRead:
        quote = await self._load_quote(session, resolved)
        items = (
            (
                await session.execute(
                    select(QuoteItem)
                    .where(
                        QuoteItem.tenant_id == resolved.tenant_id,
                        QuoteItem.quote_id == quote.id,
                    )
                    .order_by(QuoteItem.sku_snapshot)
                )
            )
            .scalars()
            .all()
        )
        seller = await self._load_seller_branding(session, resolved.tenant_id)
        order = (
            await self._load_order(session, resolved.tenant_id, quote.converted_order_id)
            if quote.converted_order_id is not None
            else None
        )
        return BuyerPortalViewRead(
            seller=seller,
            quote=PortalQuoteRead(
                quote_id=quote.id,
                quote_no=quote.quote_no,
                version=quote.version,
                status=quote.status,
                incoterm=quote.incoterm,
                port_of_loading=quote.port_of_loading,
                port_of_destination=quote.port_of_destination,
                payment_terms=quote.payment_terms,
                quote_currency=quote.quote_currency,
                fx_rate_snapshot=Decimal(str(quote.fx_rate_snapshot)),
                fx_locked_until=quote.fx_locked_until,
                freight_fee=Decimal(str(quote.freight_fee)),
                insurance_fee=Decimal(str(quote.insurance_fee)),
                customs_duty_estimate=Decimal(str(quote.customs_duty_estimate)),
                total_amount=Decimal(str(quote.total_amount)),
                # Buyer-facing projection only: `unit_cost_base_snapshot` is never read.
                items=[
                    PortalQuoteLineRead(
                        sku=item.sku_snapshot,
                        description=item.description_i18n,
                        quantity=item.quantity,
                        unit_price=Decimal(str(item.unit_price_quote_ccy)),
                        line_total=Decimal(str(item.line_total_quote_ccy)),
                    )
                    for item in items
                ],
            ),
            order=order,
            can_accept=resolved.allow_accept
            and quote.status == QuoteStatus.SENT,
            can_upload_receipt=resolved.allow_receipt_upload
            and quote.converted_order_id is not None,
            accepted_at=resolved.accepted_at,
            accepted_by_name=resolved.accepted_by_name,
        )

    async def _load_seller_branding(
        self, session: AsyncSession, tenant_id: UUID
    ) -> PortalSellerRead:
        row = (
            await session.execute(
                text(
                    "SELECT name, ai_policy_settings -> 'pi_template' AS pi_template "
                    "FROM tenants WHERE id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="tenant-not-found",
                status_code=404,
                code="ERR_TENANT_NOT_FOUND",
                title="Workspace Not Found",
                detail="This seller workspace is no longer available.",
            )
        # Only the export letterhead/bank block is projected. Raw tenant settings may
        # hold API keys and are deliberately never selected or returned.
        tenant_name = str(row._mapping["name"])
        template = parse_pi_template(tenant_name, row._mapping["pi_template"])
        return PortalSellerRead(
            company_name=template.company_legal_name or tenant_name,
            address=template.company_address,
            phone=template.contact_phone,
            email=template.contact_email,
            website=template.website,
            tax_or_reg_no=template.tax_or_reg_no,
            bank_beneficiary_name=template.bank_beneficiary_name,
            bank_name=template.bank_name,
            bank_account_no=template.bank_account_no,
            bank_swift_code=template.bank_swift_code,
            bank_address=template.bank_address,
            terms_notes=template.default_terms_notes,
        )

    async def _load_order(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> PortalOrderRead | None:
        header = (
            await session.execute(
                text(
                    "SELECT id, order_no, fulfillment_status, payment_status, "
                    "total_amount, currency FROM orders "
                    "WHERE tenant_id = :tid AND id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).mappings().one_or_none()
        if header is None:
            return None
        receivable_rows = (
            await session.execute(
                text(
                    "SELECT id, milestone_label, due_amount, paid_amount, currency, "
                    "status, due_date FROM receivables_payables "
                    "WHERE tenant_id = :tid AND order_id = :oid "
                    "AND direction = 'receivable' AND status <> 'void' "
                    "ORDER BY created_at, milestone_label"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).mappings().all()
        shipment_rows = (
            await session.execute(
                text(
                    "SELECT id, carrier_code, tracking_number, standardized_status, "
                    "sub_status, last_event_at, estimated_delivery_at, tracking_events "
                    "FROM shipments WHERE tenant_id = :tid AND order_id = :oid "
                    "ORDER BY created_at"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).mappings().all()
        return PortalOrderRead(
            order_id=header["id"],
            order_no=str(header["order_no"]),
            fulfillment_status=str(header["fulfillment_status"]),
            payment_status=str(header["payment_status"]),
            total_amount=Decimal(str(header["total_amount"])),
            currency=str(header["currency"]),
            receivables=[
                PortalReceivableRead(
                    id=row["id"],
                    milestone_label=str(row["milestone_label"]),
                    due_amount=Decimal(str(row["due_amount"])),
                    paid_amount=Decimal(str(row["paid_amount"])),
                    currency=str(row["currency"]),
                    status=str(row["status"]),
                    due_date=row["due_date"],
                )
                for row in receivable_rows
            ],
            shipments=[
                PortalShipmentRead(
                    id=row["id"],
                    carrier=row["carrier_code"],
                    tracking_number=str(row["tracking_number"]),
                    standardized_status=str(row["standardized_status"]),
                    sub_status=row["sub_status"],
                    last_event_at=row["last_event_at"],
                    estimated_delivery_at=row["estimated_delivery_at"],
                    tracking_events=_sanitize_tracking_events(row["tracking_events"]),
                )
                for row in shipment_rows
            ],
        )

    # -- buyer: accept ------------------------------------------------------

    async def accept_quote_via_portal(
        self,
        session: AsyncSession,
        resolved: ResolvedPortalToken,
        *,
        buyer_name: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> PortalAcceptResponse:
        if not resolved.allow_accept:
            raise AppError(
                slug="portal-accept-forbidden",
                status_code=403,
                code="ERR_PORTAL_ACCEPT_FORBIDDEN",
                title="Acceptance Disabled",
                detail="This portal link does not allow accepting the quote.",
            )
        clean_name = buyer_name.strip()
        if not clean_name:
            raise AppError(
                slug="buyer-name-required",
                status_code=422,
                code="ERR_VALIDATION",
                title="Buyer Name Required",
                detail="A buyer name is required to accept this quote.",
            )

        quote = (
            await session.execute(
                select(Quote)
                .where(
                    Quote.tenant_id == resolved.tenant_id, Quote.id == resolved.quote_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None:
            raise _quote_not_found_error(resolved.quote_id)

        if quote.status in {QuoteStatus.ACCEPTED, QuoteStatus.CONVERTED}:
            # Idempotent replay: the buyer double-clicked (or reloaded) the same link.
            return PortalAcceptResponse(
                quote_id=quote.id,
                quote_no=quote.quote_no,
                status=quote.status,
                accepted_at=resolved.accepted_at,
                accepted_by_name=resolved.accepted_by_name,
                order_id=quote.converted_order_id,
            )
        if quote.status != QuoteStatus.SENT:
            raise AppError(
                slug="portal-quote-not-sendable",
                status_code=409,
                code="ERR_PORTAL_QUOTE_NOT_SENDABLE",
                title="Quote Not Acceptable",
                detail=(
                    "This quote can only be accepted after the seller has sent it "
                    f"(current status '{quote.status.value}')."
                ),
            )

        quote.status = QuoteStatus.ACCEPTED
        await session.flush()
        stamped = (
            await session.execute(
                text(
                    """
                    UPDATE b2b_portal_tokens
                    SET accepted_at = COALESCE(accepted_at, now()),
                        accepted_by_name = COALESCE(accepted_by_name, :name),
                        accepted_ip = COALESCE(accepted_ip, CAST(:ip AS inet))
                    WHERE id = :id AND tenant_id = :tid
                    RETURNING accepted_at, accepted_by_name
                    """
                ),
                {
                    "id": resolved.token_id,
                    "tid": resolved.tenant_id,
                    "name": clean_name,
                    "ip": ip_address,
                },
            )
        ).one()
        await record_audit_event(
            session,
            tenant_id=resolved.tenant_id,
            actor_type="webhook",
            actor_id=f"portal:{resolved.token_id}",
            action="quote.accepted_via_portal",
            resource_type="quote",
            resource_id=quote.id,
            before_state={"status": QuoteStatus.SENT.value},
            after_state={
                "status": quote.status.value,
                "buyer_name": clean_name,
                "portal_token_id": str(resolved.token_id),
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return PortalAcceptResponse(
            quote_id=quote.id,
            quote_no=quote.quote_no,
            status=quote.status,
            accepted_at=stamped.accepted_at,
            accepted_by_name=stamped.accepted_by_name,
            order_id=quote.converted_order_id,
        )

    # -- buyer: T/T receipt -------------------------------------------------

    async def submit_buyer_receipt(
        self,
        session: AsyncSession,
        storage: ReceiptStoragePort | None,
        resolved: ResolvedPortalToken,
        *,
        rp_id: UUID,
        payment_method: str,
        external_tx_ref: str | None,
        amount_received: Decimal,
        currency: str,
        receipt_bytes: bytes | None,
        receipt_filename: str | None,
        receipt_content_type: str | None,
        ip_address: str | None,
    ) -> PortalReceiptResponse:
        if not resolved.allow_receipt_upload:
            raise AppError(
                slug="portal-receipt-forbidden",
                status_code=403,
                code="ERR_PORTAL_RECEIPT_FORBIDDEN",
                title="Receipt Upload Disabled",
                detail="This portal link does not allow uploading payment receipts.",
            )
        quote = await self._load_quote(session, resolved)
        if quote.converted_order_id is None:
            raise AppError(
                slug="portal-quote-not-converted",
                status_code=409,
                code="ERR_PORTAL_QUOTE_NOT_CONVERTED",
                title="Quote Not Converted",
                detail="Payment milestones appear once the seller converts this quote to an order.",
            )
        order_id = quote.converted_order_id

        receivable = (
            await session.execute(
                text(
                    "SELECT id, order_id, milestone_label, due_amount, paid_amount, "
                    "currency, status FROM receivables_payables "
                    "WHERE tenant_id = :tid AND id = :rp AND order_id = :oid "
                    "AND direction = 'receivable'"
                ),
                {"tid": resolved.tenant_id, "rp": rp_id, "oid": order_id},
            )
        ).mappings().one_or_none()
        if receivable is None:
            raise AppError(
                slug="receivable-not-found",
                status_code=404,
                code="ERR_RECEIVABLE_NOT_FOUND",
                title="Payment Milestone Not Found",
                detail="This payment milestone does not belong to the order on this link.",
            )
        if receivable["status"] in {"settled", "void"}:
            raise AppError(
                slug="receivable-already-settled",
                status_code=409,
                code="ERR_RECEIVABLE_ALREADY_SETTLED",
                title="Milestone Already Settled",
                detail=(
                    f"This milestone is already '{receivable['status']}' and cannot "
                    "accept another bank slip."
                ),
            )

        normalized_currency = currency.strip().upper()
        rp_currency = str(receivable["currency"]).upper()
        if normalized_currency != rp_currency:
            raise AppError(
                slug="payment-currency-mismatch",
                status_code=422,
                code="ERR_PAYMENT_CURRENCY_MISMATCH",
                title="Payment Currency Mismatch",
                detail=f"This milestone must be paid in {rp_currency}.",
            )

        try:
            amount = _cent(Decimal(amount_received))
        except (InvalidOperation, ValueError):
            raise AppError(
                slug="invalid-payment-amount",
                status_code=422,
                code="ERR_VALIDATION",
                title="Invalid Payment Amount",
                detail="The transferred amount must be a valid decimal.",
            ) from None
        if amount <= Decimal("0.00"):
            raise AppError(
                slug="invalid-payment-amount",
                status_code=422,
                code="ERR_VALIDATION",
                title="Invalid Payment Amount",
                detail="The transferred amount must be greater than zero.",
            )
        outstanding = _cent(
            Decimal(str(receivable["due_amount"])) - Decimal(str(receivable["paid_amount"]))
        )
        if amount > outstanding + Decimal("0.01"):
            raise AppError(
                slug="receivable-overpayment",
                status_code=422,
                code="ERR_RECEIVABLE_OVERPAYMENT",
                title="Overpayment Rejected",
                detail="The transferred amount exceeds the remaining milestone balance.",
            )

        clean_ref = (external_tx_ref or "").strip() or None
        if clean_ref is not None:
            duplicate = await session.scalar(
                text(
                    "SELECT 1 FROM payment_records "
                    "WHERE tenant_id = :tid AND external_tx_ref = :ref LIMIT 1"
                ),
                {"tid": resolved.tenant_id, "ref": clean_ref},
            )
            if duplicate is not None:
                raise _duplicate_ref_error(clean_ref)

        receipt_key: str | None = None
        if receipt_bytes:
            content_type = (receipt_content_type or "").strip().lower()
            if content_type not in ALLOWED_RECEIPT_CONTENT_TYPES:
                raise AppError(
                    slug="portal-receipt-type-invalid",
                    status_code=422,
                    code="ERR_PORTAL_RECEIPT_TYPE_INVALID",
                    title="Unsupported Receipt File",
                    detail="Upload a PDF, PNG, JPEG or WebP bank slip.",
                )
            if len(receipt_bytes) > MAX_RECEIPT_BYTES:
                raise AppError(
                    slug="portal-receipt-too-large",
                    status_code=422,
                    code="ERR_PORTAL_RECEIPT_TOO_LARGE",
                    title="Receipt File Too Large",
                    detail="The bank slip must be 5 MB or smaller.",
                )
            if storage is None:
                raise AppError(
                    slug="portal-storage-unavailable",
                    status_code=503,
                    code="ERR_PORTAL_STORAGE_UNAVAILABLE",
                    title="Storage Unavailable",
                    detail="Receipt storage is not configured for this workspace.",
                )
            safe_name = _safe_filename(receipt_filename, content_type)
            receipt_key = (
                f"receipts/{resolved.tenant_id}/portal/{uuid4().hex}_{safe_name}"
            )
            await storage.upload_bytes(receipt_key, receipt_bytes, content_type)

        actual_fx_rate = await self._resolve_receipt_fx_rate(
            session, resolved.tenant_id, quote, normalized_currency
        )
        payment_id = await self._payments.create_unverified_payment(
            session,
            tenant_id=resolved.tenant_id,
            rp_id=rp_id,
            payment_method=payment_method.strip() or "bank_transfer",
            external_tx_ref=clean_ref,
            amount_received=amount,
            currency=normalized_currency,
            actual_fx_rate=actual_fx_rate,
            receipt_r2_key=receipt_key,
            paid_at=datetime.now(UTC),
        )
        await record_audit_event(
            session,
            tenant_id=resolved.tenant_id,
            actor_type="webhook",
            actor_id=f"portal:{resolved.token_id}",
            action="payment.submitted_via_portal",
            resource_type="payment_record",
            resource_id=payment_id,
            after_state={
                "rp_id": str(rp_id),
                "order_id": str(order_id),
                "amount_received": str(amount),
                "currency": normalized_currency,
                "external_tx_ref": clean_ref,
                "receipt_r2_key": receipt_key,
                # The buyer upload is only a claim; Finance L0 verification settles AR.
                "verified_by_user_id": None,
                "portal_token_id": str(resolved.token_id),
            },
            ip_address=ip_address,
        )
        return PortalReceiptResponse(
            payment_id=payment_id,
            rp_id=rp_id,
            order_id=order_id,
            amount_received=amount,
            currency=normalized_currency,
            receipt_r2_key=receipt_key,
        )

    async def _resolve_receipt_fx_rate(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        quote: Quote,
        payment_currency: str,
    ) -> Decimal:
        base_currency = await session.scalar(
            text("SELECT base_currency FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )
        base = str(base_currency).upper() if base_currency else None
        actual_fx_rate = (
            Decimal("1.000000")
            if base is not None and payment_currency == base
            else Decimal(str(quote.fx_rate_snapshot))
        )
        from erp_core.finance.payment_service import validate_same_currency_fx_rate

        return validate_same_currency_fx_rate(
            payment_currency=payment_currency,
            base_currency=base,
            actual_fx_rate=actual_fx_rate,
        )

    # -- buyer: documents ---------------------------------------------------

    async def render_buyer_document(
        self,
        session: AsyncSession,
        resolved: ResolvedPortalToken,
        doc_type: DocumentType,
        language: str,
    ) -> tuple[bytes, str]:
        quote = await self._load_quote(session, resolved)
        normalized_language = "zh" if language.strip().lower().startswith("zh") else "en"
        if doc_type == "pi":
            pdf_bytes, rendered = await self._quotes.render_pi_pdf_bytes(
                session, resolved.tenant_id, quote.id, language=normalized_language
            )
            return pdf_bytes, QuoteService.pi_pdf_filename(rendered)

        if quote.converted_order_id is None:
            raise AppError(
                slug="portal-quote-not-converted",
                status_code=409,
                code="ERR_PORTAL_QUOTE_NOT_CONVERTED",
                title="Document Not Available",
                detail=(
                    "Commercial invoice and packing list are issued once the seller "
                    "converts this quote to an order."
                ),
            )
        # Lazy import keeps `erp_core.crm` free of module-level `orders` coupling.
        from erp_core.orders.documents import (
            ExportDocumentService,
            render_commercial_invoice_pdf,
            render_packing_list_pdf,
            safe_filename,
        )

        ctx = await ExportDocumentService().build_context(
            session,
            resolved.tenant_id,
            quote.converted_order_id,
            language=normalized_language,
        )
        if doc_type == "ci":
            pdf_bytes = await asyncio.to_thread(render_commercial_invoice_pdf, ctx)
            return pdf_bytes, safe_filename("CI", ctx.order_no)
        pdf_bytes = await asyncio.to_thread(render_packing_list_pdf, ctx)
        return pdf_bytes, safe_filename("PL", ctx.order_no)
