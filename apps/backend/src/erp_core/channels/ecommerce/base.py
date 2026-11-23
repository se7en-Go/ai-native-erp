"""Shared storefront connector contract: normalized DTOs, credential vault, order sync."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.identity.service import IdentityResolverService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal, OrderRead
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

#: Order -> tenant-base FX rates are locked at 6 decimal places (orders.fx_rate_to_base).
FX_RATE_SCALE = Decimal("0.000001")

#: USD-base reference rates (units of each currency per 1 USD). These are ONLY used when
#: the caller has no live quote at hand: an ecommerce order must never inherit an invented
#: rate such as a blanket 7.2 for CNY/EUR/GBP orders. Values mirror
#: ``erp_core.finance.fx_service._FALLBACK_USD_RATES`` and are pinned by a regression test.
USD_BASE_REFERENCE_RATES: Mapping[str, Decimal] = {
    "USD": Decimal("1.0000"),
    "CNY": Decimal("7.2400"),
    "EUR": Decimal("0.9200"),
    "GBP": Decimal("0.7900"),
    "JPY": Decimal("154.0000"),
    "HKD": Decimal("7.7800"),
}


def _quantize_fx_rate(value: Decimal) -> Decimal:
    return value.quantize(FX_RATE_SCALE, rounding=ROUND_HALF_UP)


def attach_financial_metadata(
    metadata: dict[str, Any],
    *,
    refunded_amount: Decimal | None = None,
    platform_commission_base: Decimal | None = None,
) -> None:
    """Persist exact 2dp Decimal strings into ``channel_metadata`` when non-negative/present."""
    if refunded_amount is not None and refunded_amount > Decimal("0"):
        metadata["refunded_amount"] = str(
            refunded_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    if platform_commission_base is not None and platform_commission_base > Decimal("0"):
        metadata["platform_commission_base"] = str(
            platform_commission_base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )


def _units_per_usd(currency_code: str, usd_base_rates: Mapping[str, Decimal]) -> Decimal:
    """Return how many `currency_code` units one USD buys."""
    if currency_code == "USD":
        return Decimal("1")
    raw = usd_base_rates.get(currency_code)
    quoted = Decimal(str(raw)) if raw is not None else None
    if quoted is None or quoted <= Decimal("0"):
        raise AppError(
            slug="unsupported-fx-currency",
            status_code=422,
            code="ERR_UNSUPPORTED_FX_CURRENCY",
            title="Unsupported FX Currency",
            detail=(
                f"No FX quote is available for currency '{currency_code}'; refusing to guess "
                f"an order-to-base rate."
            ),
        )
    return quoted


def resolve_order_fx_rate(
    *,
    order_currency: str,
    base_currency: str,
    explicit_rate: Decimal | None,
    live_rates_usd_base: Mapping[str, Decimal] | None = None,
) -> tuple[Decimal, str]:
    """Resolve the order -> tenant-base FX rate and tag where it came from.

    ``rate`` multiplies order-currency amounts into the tenant base currency. ``source`` is
    one of ``explicit`` (channel/order supplied), ``identity`` (same currency),
    ``live_fx`` (caller supplied USD-base quotes) or ``fallback_fx`` (reference table).

    A blanket default (the former 7.2) is deliberately impossible: cross rates are always
    derived from USD-base quotes as ``quote(base) / quote(order)``.
    """
    if explicit_rate is not None:
        explicit = Decimal(str(explicit_rate))
        if explicit <= Decimal("0"):
            raise AppError(
                slug="invalid-order-fx-rate",
                status_code=422,
                code="ERR_INVALID_ORDER_FX_RATE",
                title="Invalid Order FX Rate",
                detail="An explicit order FX rate must be greater than zero.",
            )
        return _quantize_fx_rate(explicit), "explicit"

    order_code = order_currency.strip().upper()
    base_code = base_currency.strip().upper()
    if len(order_code) != 3 or len(base_code) != 3:
        raise AppError(
            slug="invalid-currency-code",
            status_code=422,
            code="ERR_INVALID_CURRENCY_CODE",
            title="Invalid Currency Code",
            detail=(
                "Order and base currency must be ISO-4217 three-letter codes "
                f"(got order='{order_currency}', base='{base_currency}')."
            ),
        )
    if order_code == base_code:
        return _quantize_fx_rate(Decimal("1")), "identity"

    has_live_rates = live_rates_usd_base is not None
    usd_base_rates: Mapping[str, Decimal] = (
        live_rates_usd_base if live_rates_usd_base is not None else USD_BASE_REFERENCE_RATES
    )
    rate = _quantize_fx_rate(
        _units_per_usd(base_code, usd_base_rates) / _units_per_usd(order_code, usd_base_rates)
    )
    if rate <= Decimal("0"):
        raise AppError(
            slug="invalid-order-fx-rate",
            status_code=422,
            code="ERR_INVALID_ORDER_FX_RATE",
            title="Invalid Order FX Rate",
            detail=f"Resolved a non-positive {order_code}->{base_code} FX rate.",
        )
    return rate, ("live_fx" if has_live_rates else "fallback_fx")


class NormalizedLineItem(BaseModel):
    external_sku: str
    title: str = ""
    quantity: int = Field(gt=0)
    line_total: Decimal


class NormalizedCustomer(BaseModel):
    email: str | None = None
    phone_e164: str | None = None
    external_customer_id: str | None = None
    external_identity_type: IdentityType = IdentityType.EMAIL
    display_name: str = "Unknown Buyer"
    country_code: str | None = None


class NormalizedB2BCompany(BaseModel):
    legal_name: str
    country_code: str = "US"
    external_company_id: str | None = None
    location_id: str | None = None


class NormalizedOrder(BaseModel):
    external_order_id: str
    external_order_number: str | None = None
    order_mode: OrderMode = OrderMode.B2C_RETAIL
    currency: str = "USD"
    #: Locked at ingestion by `resolve_order_fx_rate`; None means "resolve it, never guess".
    fx_rate_to_base: Decimal | None = None
    subtotal_amount: Decimal
    shipping_charged: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    tax_collected: Decimal = Decimal("0.00")
    total_amount: Decimal
    platform_commission_base: Decimal = Decimal("0.00")
    fulfillment_status: FulfillmentStatus = FulfillmentStatus.CONFIRMED_RESERVED
    payment_status: PaymentStatus = PaymentStatus.PAID
    customer: NormalizedCustomer
    b2b_company: NormalizedB2BCompany | None = None
    line_items: list[NormalizedLineItem] = Field(min_length=1)
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class NormalizedProduct(BaseModel):
    external_product_id: str
    title: str
    variants: list[dict[str, Any]] = Field(default_factory=list)


class CommercePlatformAdapter(ABC):
    """Unified interface implemented by every storefront connector."""

    @abstractmethod
    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]: ...

    @abstractmethod
    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]: ...

    @abstractmethod
    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool: ...


async def register_channel_routing(
    session: AsyncSession,
    *,
    channel_type: str,
    external_account_id: str,
    tenant_id: UUID,
    channel_account_id: UUID,
    is_active: bool = True,
) -> None:
    """Bind a store domain to a tenant through the validated SECURITY DEFINER function."""
    await session.execute(
        text(
            "SELECT register_channel_routing("
            "CAST(:ctype AS text), CAST(:ext AS text), CAST(:tid AS uuid), "
            "CAST(:caid AS uuid), :active)"
        ),
        {
            "ctype": channel_type,
            "ext": external_account_id,
            "tid": tenant_id,
            "caid": channel_account_id,
            "active": is_active,
        },
    )


async def resolve_routing_tenant(
    session: AsyncSession, channel_type: str, external_account_id: str
) -> tuple[UUID, UUID] | None:
    """Resolve a store domain to (tenant_id, channel_account_id) for signed webhooks."""
    row = (
        await session.execute(
            text(
                "SELECT tenant_id, channel_account_id "
                "FROM resolve_tenant_by_channel_external_id("
                "CAST(:ctype AS text), CAST(:ext AS text))"
            ),
            {"ctype": channel_type, "ext": external_account_id},
        )
    ).one_or_none()
    return (row.tenant_id, row.channel_account_id) if row is not None else None


class CredentialVaultHelper:
    """Envelope-encrypted JSON credentials bound to AAD(tenant:account:type)."""

    def __init__(self, crypto: EnvelopeCryptoService | None = None) -> None:
        self.crypto = crypto or EnvelopeCryptoService(get_settings())

    async def save_secret_json(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        credential_type: str,
        payload: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> None:
        plaintext = json.dumps(payload, separators=(",", ":"))
        ciphertext, nonce, auth_tag, key_version = await self.crypto.encrypt_secret(
            session, tenant_id, channel_account_id, credential_type, plaintext
        )
        await session.execute(
            text(
                """
                INSERT INTO channel_credentials (
                    id, tenant_id, channel_account_id, credential_type,
                    ciphertext, nonce, auth_tag, key_version, expires_at, updated_at
                ) VALUES (
                    :id, :tid, :caid, :ctype, :ct, :nonce, :tag, :kver, :exp, now()
                )
                ON CONFLICT (tenant_id, channel_account_id, credential_type)
                DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    nonce = EXCLUDED.nonce,
                    auth_tag = EXCLUDED.auth_tag,
                    key_version = EXCLUDED.key_version,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                """
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "caid": channel_account_id,
                "ctype": credential_type,
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kver": key_version,
                "exp": expires_at,
            },
        )

    async def load_secret_json(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        credential_type: str,
    ) -> tuple[dict[str, Any], datetime | None]:
        row = (
            await session.execute(
                text(
                    "SELECT ciphertext, nonce, auth_tag, key_version, expires_at "
                    "FROM channel_credentials "
                    "WHERE tenant_id = :tid AND channel_account_id = :caid "
                    "AND credential_type = :ctype"
                ),
                {"tid": tenant_id, "caid": channel_account_id, "ctype": credential_type},
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="credential-not-found",
                status_code=404,
                code="ERR_CREDENTIAL_NOT_FOUND",
                title="Credential Not Found",
                detail=(
                    f"Missing '{credential_type}' credential for channel account "
                    f"'{channel_account_id}'."
                ),
            )
        plaintext = await self.crypto.decrypt_secret(
            session,
            tenant_id,
            channel_account_id,
            credential_type,
            bytes(row.ciphertext),
            bytes(row.nonce),
            bytes(row.auth_tag),
            int(row.key_version),
        )
        return json.loads(plaintext), row.expires_at


class EcommerceOrderSyncService:
    """Idempotent ingestion of normalized storefront orders into the ERP core."""

    def __init__(
        self,
        identity_service: IdentityResolverService | None = None,
        catalog_service: CatalogService | None = None,
        order_service: OrderService | None = None,
        payment_service: PaymentService | None = None,
        profit_service: OrderProfitService | None = None,
    ) -> None:
        self.identity = identity_service or IdentityResolverService()
        self.catalog = catalog_service or CatalogService()
        self.orders = order_service or OrderService(catalog_service=self.catalog)
        self.payments = payment_service or PaymentService(order_service=self.orders)
        self.profits = profit_service or OrderProfitService()

    @staticmethod
    async def _tenant_base_currency(session: AsyncSession, tenant_id: UUID) -> str:
        """Tenant reporting currency used to lock every imported order's FX rate."""
        value = await session.scalar(
            text("SELECT base_currency FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )
        code = str(value or "").strip().upper()
        return code if len(code) == 3 else "CNY"

    async def _apply_existing_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_dto: NormalizedOrder,
        order_id: UUID,
    ) -> tuple[UUID, bool]:
        """Refresh a known storefront order (idempotent replay / duplicate-race loser)."""
        await self.orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            fulfillment_status=order_dto.fulfillment_status,
            payment_status=order_dto.payment_status,
            channel_metadata_patch=order_dto.channel_metadata,
        )
        await self.profits.recalculate_order_profit(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            platform_commission_base=order_dto.platform_commission_base,
        )
        return order_id, False

    async def _existing_order_id(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        order_dto: NormalizedOrder,
    ) -> UUID:
        """Re-read the winner of a concurrent insert, or fail closed with a retryable 409."""
        existing = await self.orders.find_by_external_order(
            session, tenant_id, channel_account_id, order_dto.external_order_id
        )
        if existing is None:
            raise AppError(
                slug="external-order-race-missing",
                status_code=409,
                code="ERR_EXTERNAL_ORDER_RACE_MISSING",
                title="External Order Reconciliation Conflict",
                detail=(
                    f"Order '{order_dto.external_order_id}' was inserted concurrently but is "
                    "not visible in this transaction; the next reconciliation pass will "
                    "pick it up."
                ),
            )
        return existing.id

    async def _create_order_or_delegate(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        channel_account_id: UUID,
        default_warehouse_id: UUID,
        order_dto: NormalizedOrder,
        actor_id: str,
        customer_id: UUID,
        fx_rate_to_base: Decimal,
        channel_metadata: dict[str, Any],
        order_items: list[OrderItemInternal],
    ) -> OrderRead | None:
        """Insert the order inside a savepoint, or return None when a concurrent ingest won.

        An order imported for the first time already cancelled/refunded must not reserve stock:
        nothing will ever ship, so the reservation would strand available quantity forever.
        """
        reserve_stock = order_dto.fulfillment_status not in {
            FulfillmentStatus.CANCELLED,
            FulfillmentStatus.REFUNDED,
        }
        try:
            async with session.begin_nested():
                return await self.orders.create_order_atomic(
                    session,
                    tenant_id=tenant_id,
                    actor_type="webhook",
                    actor_id=actor_id,
                    payload=OrderCreateInternal(
                        order_mode=order_dto.order_mode,
                        channel_account_id=channel_account_id,
                        external_order_id=order_dto.external_order_id,
                        customer_id=customer_id,
                        warehouse_id=default_warehouse_id,
                        currency=order_dto.currency.upper(),
                        fx_rate_to_base=fx_rate_to_base,
                        subtotal_amount=order_dto.subtotal_amount,
                        shipping_charged=order_dto.shipping_charged,
                        discount_amount=order_dto.discount_amount,
                        tax_collected=order_dto.tax_collected,
                        total_amount=order_dto.total_amount,
                        initial_fulfillment_status=order_dto.fulfillment_status,
                        initial_payment_status=order_dto.payment_status,
                        channel_metadata=channel_metadata,
                        items=order_items,
                    ),
                    reserve_stock=reserve_stock,
                )
        except IntegrityError:
            logger.warning(
                "ecommerce_order_duplicate_race_delegated_to_update",
                tenant_id=str(tenant_id),
                channel_account_id=str(channel_account_id),
                external_order_id=order_dto.external_order_id,
            )
            return None

    async def ingest_normalized_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        default_warehouse_id: UUID,
        order_dto: NormalizedOrder,
        actor_id: str = "ecommerce_sync",
    ) -> tuple[UUID, bool]:
        """Idempotently import one storefront order, locking an honest FX rate."""
        existing = await self.orders.find_by_external_order(
            session, tenant_id, channel_account_id, order_dto.external_order_id
        )
        if existing is not None:
            return await self._apply_existing_order(session, tenant_id, order_dto, existing.id)

        if order_dto.customer.email:
            id_type, id_val = IdentityType.EMAIL, order_dto.customer.email
        elif order_dto.customer.phone_e164:
            id_type, id_val = IdentityType.PHONE_E164, order_dto.customer.phone_e164
        elif order_dto.customer.external_customer_id:
            id_type = order_dto.customer.external_identity_type
            id_val = order_dto.customer.external_customer_id
        else:
            id_type, id_val = (
                IdentityType.WEB_VISITOR_ID,
                f"ext-order:{order_dto.external_order_id}",
            )
        customer_id, _ = await self.identity.resolve_or_create_customer(
            session,
            tenant_id=tenant_id,
            identity_type=id_type,
            raw_value=id_val,
            display_name=order_dto.customer.display_name,
            country_hint=order_dto.customer.country_code,
        )

        if order_dto.b2b_company is not None:
            company_id = await session.scalar(
                text(
                    "SELECT id FROM b2b_companies "
                    "WHERE tenant_id = :tid AND legal_name = :lname LIMIT 1"
                ),
                {"tid": tenant_id, "lname": order_dto.b2b_company.legal_name},
            )
            if company_id is None:
                company_id = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO b2b_companies "
                        "(id, tenant_id, legal_name, country_code, default_incoterm) "
                        "VALUES (:id, :tid, :lname, :cc, 'FOB')"
                    ),
                    {
                        "id": company_id,
                        "tid": tenant_id,
                        "lname": order_dto.b2b_company.legal_name,
                        "cc": order_dto.b2b_company.country_code[:2].upper(),
                    },
                )
            await session.execute(
                text(
                    "UPDATE customers SET company_id = COALESCE(company_id, :cid), "
                    "customer_type = 'b2b_contact' WHERE tenant_id = :tid AND id = :cust_id"
                ),
                {"cid": company_id, "tid": tenant_id, "cust_id": customer_id},
            )

        base_currency = await self._tenant_base_currency(session, tenant_id)
        fx_rate_to_base, fx_source = resolve_order_fx_rate(
            order_currency=order_dto.currency,
            base_currency=base_currency,
            explicit_rate=order_dto.fx_rate_to_base,
        )
        channel_metadata = {
            **order_dto.channel_metadata,
            "fx_source": fx_source,
            "fx_rate_order_to_base": str(fx_rate_to_base),
            "order_currency": order_dto.currency.strip().upper(),
            "base_currency": base_currency,
        }
        if order_dto.platform_commission_base > Decimal("0"):
            attach_financial_metadata(
                channel_metadata,
                platform_commission_base=order_dto.platform_commission_base,
            )

        order_items: list[OrderItemInternal] = []
        for line in order_dto.line_items:
            resolved = await self.catalog.resolve_sku_for_channel(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                external_sku=line.external_sku,
                external_qty=line.quantity,
            )
            if resolved is None:
                raise AppError(
                    slug="unmapped-channel-sku",
                    status_code=422,
                    code="ERR_UNMAPPED_CHANNEL_SKU",
                    title="Unmapped Channel SKU",
                    detail=(
                        f"External SKU '{line.external_sku}' is not mapped for channel "
                        f"account '{channel_account_id}'."
                    ),
                )
            unit_price = (line.line_total / Decimal(resolved.deduct_qty)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            order_items.append(
                OrderItemInternal(
                    variant_id=resolved.variant_id,
                    sku_snapshot=resolved.internal_sku,
                    quantity=resolved.deduct_qty,
                    unit_price=unit_price,
                    unit_cost_base_snapshot=resolved.cost_price_base,
                    line_total=line.line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                )
            )

        created_order = await self._create_order_or_delegate(
            session,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            default_warehouse_id=default_warehouse_id,
            order_dto=order_dto,
            actor_id=actor_id,
            customer_id=customer_id,
            fx_rate_to_base=fx_rate_to_base,
            channel_metadata=channel_metadata,
            order_items=order_items,
        )
        if created_order is None:
            # A concurrent ingest won the external_order_id unique index race: the savepoint
            # rolled our insert back, so this delivery becomes an update of the winner's row.
            return await self._apply_existing_order(
                session,
                tenant_id,
                order_dto,
                await self._existing_order_id(
                    session, tenant_id, channel_account_id, order_dto
                ),
            )

        is_paid = order_dto.payment_status == PaymentStatus.PAID
        await self.payments.create_single_receivable(
            session,
            tenant_id=tenant_id,
            order_id=created_order.id,
            milestone_label="full_prepaid",
            due_amount=created_order.total_amount,
            currency=created_order.currency,
            status="settled" if is_paid else "open",
            paid_amount=created_order.total_amount if is_paid else Decimal("0.00"),
        )
        await self.profits.recalculate_order_profit(
            session,
            tenant_id=tenant_id,
            order_id=created_order.id,
            platform_commission_base=order_dto.platform_commission_base,
        )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id=actor_id,
            action="ecommerce.order_imported",
            resource_type="order",
            resource_id=created_order.id,
            after_state={
                "external_order_id": order_dto.external_order_id,
                "order_no": created_order.order_no,
            },
        )
        return created_order.id, True
