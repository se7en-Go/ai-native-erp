"""Walmart Marketplace connector: OAuth2 client-credentials, orders, items, shipping.

Deterministic and offline-testable by construction: the adapter owns an
``httpx.AsyncBaseTransport`` seam (``httpx.MockTransport`` in tests), every monetary amount
is parsed into ``Decimal`` and quantized with ``ROUND_HALF_UP`` (never ``float``), and no
credential material (``client_secret`` / ``access_token``) can reach ``repr``, a log record
or an ``AppError`` detail.

Walmart endpoints used (US Marketplace, ``/v3``):

* ``POST /v3/token`` — ``grant_type=client_credentials`` with HTTP Basic
  ``client_id:client_secret``; returned tokens live 15 minutes.
* ``GET  /v3/orders`` — ``lastModifiedStartDate`` feed with an opaque ``nextCursor``.
* ``GET  /v3/orders/{purchaseOrderId}`` then ``POST /v3/orders/{purchaseOrderId}/shipping``
  — Walmart rejects a shipment that does not name every ``lineNumber`` it is claiming.
* ``GET  /v3/items`` — item feed (no modified-since filter exists; walked by ``offset``).
* ``POST /v3/feeds?feedType=MP_ITEM`` — asynchronous item listing creation.
* ``PUT  /v3/inventory?sku=...`` — single-node sellable quantity push.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NoReturn
from urllib.parse import quote
from uuid import uuid4

import httpx
import structlog

from erp_core.catalog.sourcing_service import (
    MarketplaceListingDispatchReceipt,
    MarketplaceListingPayload,
    MarketplaceVariantReceipt,
)
from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
    attach_financial_metadata,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

logger = structlog.get_logger(__name__)

WALMART_PRODUCTION_BASE_URL = "https://marketplace.walmartapis.com"
WALMART_SANDBOX_BASE_URL = "https://sandbox.walmartapis.com"
WALMART_SERVICE_NAME = "Walmart Marketplace"

WALMART_TOKEN_PATH = "/v3/token"
WALMART_ORDERS_PATH = "/v3/orders"
WALMART_ITEMS_PATH = "/v3/items"
WALMART_FEEDS_PATH = "/v3/feeds"
WALMART_INVENTORY_PATH = "/v3/inventory"

#: Walmart caps the orders/items feeds at 200 rows per page.
WALMART_PAGE_LIMIT = 200
#: Walmart OAuth tokens expire after 15 minutes.
WALMART_DEFAULT_TOKEN_TTL_SECONDS = 900
#: Refresh a minute early so a live call never races the platform's own expiry.
WALMART_TOKEN_EXPIRY_BUFFER_SECONDS = 60.0
#: Walmart truncates product names above 100 characters.
WALMART_MAX_TITLE_LENGTH = 100
#: Walmart accepts up to 20 image URLs per item; the first one is the main image.
WALMART_MAX_IMAGES = 20
WALMART_INVENTORY_UNIT = "EACH"
#: ``MarketplaceListingPayload.price_usd`` is USD by contract; the feed must say so.
WALMART_LISTING_CURRENCY = "USD"
WALMART_FEED_TYPE = "MP_ITEM"
WALMART_FEED_LOCALE = "en"
WALMART_FEED_VERSION = "3.0"

MONEY_SCALE = Decimal("0.01")

#: Signature headers Walmart (and its 2026 ``ORDER_UPDATES`` webhooks) are seen using.
_SIGNATURE_HEADERS: tuple[str, ...] = ("wm-sec.signature", "x-walmart-signature")

#: Walmart order-line status -> internal fulfillment status.
_ORDER_LINE_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "CREATED": FulfillmentStatus.CONFIRMED_RESERVED,
    "ACKNOWLEDGED": FulfillmentStatus.CONFIRMED_RESERVED,
    "SHIPPED": FulfillmentStatus.SHIPPED,
    "DELIVERED": FulfillmentStatus.DELIVERED,
    "CANCELLED": FulfillmentStatus.CANCELLED,
    "CANCELED": FulfillmentStatus.CANCELLED,
    "REFUND": FulfillmentStatus.REFUNDED,
    "REFUNDED": FulfillmentStatus.REFUNDED,
    "RETURNED": FulfillmentStatus.REFUNDED,
}

#: Carriers Walmart recognises by code; anything else rides the ``Other`` enum plus a name.
_KNOWN_CARRIERS: Mapping[str, str] = {
    "UPS": "UPS",
    "USPS": "USPS",
    "FEDEX": "FedEx",
    "FED EX": "FedEx",
    "ONTRAC": "OnTrac",
    "DHL": "DHL",
    "AIRBORNE": "Airborne",
}

_SHIPPED_LIKE = frozenset({FulfillmentStatus.SHIPPED, FulfillmentStatus.DELIVERED})
_DEAD_LINE = frozenset({FulfillmentStatus.CANCELLED, FulfillmentStatus.REFUNDED})


def _mapping(value: Any) -> Mapping[str, Any]:
    """Read a JSON object defensively (Walmart omits keys on every nesting level)."""
    return value if isinstance(value, Mapping) else {}


def _header(headers: Mapping[str, str], wanted: str) -> str:
    """Case-insensitive header lookup — webhook headers arrive in any casing."""
    for key, value in headers.items():
        if str(key).strip().lower() == wanted:
            return str(value).strip()
    return ""


def verify_walmart_webhook(
    raw_body: bytes, headers: Mapping[str, str], client_secret: str
) -> bool:
    """Fail-closed constant-time HMAC-SHA256 check of a Walmart webhook signature.

    Walmart signs the raw request body with the app's ``client_secret`` and ships the digest
    in ``wm-sec.signature`` (or ``x-walmart-signature``). Both hex and base64 encodings are
    in the wild, so both are accepted — an empty secret, body or signature is always False.
    """
    signature = ""
    for name in _SIGNATURE_HEADERS:
        signature = _header(headers, name)
        if signature:
            break
    if not client_secret or not signature or not raw_body:
        return False
    if signature.lower().startswith("sha256="):
        signature = signature[len("sha256=") :].strip()
    digest = hmac.new(client_secret.encode(), raw_body, hashlib.sha256).digest()
    encoded = base64.b64encode(digest).decode()
    for expected in (digest.hex(), encoded, encoded.rstrip("=")):
        if hmac.compare_digest(expected, signature) or hmac.compare_digest(
            expected, signature.lower()
        ):
            return True
    return False


class WalmartAdapter(CommercePlatformAdapter):
    """Walmart Marketplace adapter (orders, items, shipping, feeds, inventory)."""

    def __init__(
        self,
        *,
        seller_id: str,
        client_id: str,
        client_secret: str,
        access_token: str | None = None,
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.seller_id = seller_id
        self.fulfillment_mode = (fulfillment_mode or "standard").strip().lower() or "standard"
        self.base_url = (endpoint_url or WALMART_PRODUCTION_BASE_URL).rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._token_lock = asyncio.Lock()
        # A caller-supplied token has no advertised TTL: trust it and never spend a round
        # trip guessing when it dies. A 401 then fails closed instead of silently retrying.
        self._token_expires_at = float("inf") if access_token else 0.0

    def __repr__(self) -> str:
        return (
            f"WalmartAdapter(seller_id={self.seller_id!r}, "
            f"fulfillment_mode={self.fulfillment_mode!r}, base_url={self.base_url!r})"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout_seconds, trust_env=False
        )

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a log line or an ``AppError`` detail."""
        return tuple(
            value
            for value in (self._client_secret, self._access_token or "")
            if value
        )

    def _redacted(self, text: str, limit: int = 400) -> str:
        cleaned = text[:limit]
        for secret in self._secrets():
            cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned

    def _error_summary(self, response: httpx.Response) -> str:
        return f"HTTP {response.status_code}: {self._redacted(response.text)}".strip()

    def _raise_api_error(
        self, response: httpx.Response, *, slug: str, code: str, title: str
    ) -> NoReturn:
        raise AppError(
            slug=slug,
            status_code=502,
            code=code,
            title=title,
            detail=self._error_summary(response),
        )

    @staticmethod
    def _raise_for_transport_status(response: httpx.Response) -> None:
        """Map the credential/rate outcomes Walmart expects callers to handle explicitly."""
        if response.status_code in (401, 403):
            raise AppError(
                slug="walmart-auth-failed",
                status_code=502,
                code="ERR_WALMART_AUTH_FAILED",
                title="Walmart Authentication Failed",
                detail=(
                    "Walmart rejected the marketplace credentials "
                    f"(HTTP {response.status_code})."
                ),
            )
        if response.status_code == 429:
            raise AppError(
                slug="walmart-rate-limited",
                status_code=429,
                code="ERR_WALMART_RATE_LIMITED",
                title="Walmart Rate Limited",
                detail=(
                    "Walmart returned HTTP 429 for the Marketplace call; retry after the "
                    "advertised token-bucket replenishment."
                ),
            )

    def _base_headers(self) -> dict[str, str]:
        return {
            "WM_SVC.NAME": WALMART_SERVICE_NAME,
            "WM_QOS.CORRELATION_ID": uuid4().hex,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        token = await self._access_token_or_refresh()
        headers = {**self._base_headers(), "WM_SEC.ACCESS_TOKEN": token}
        async with self._client() as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=dict(params) if params is not None else None,
                    json=dict(json_body) if json_body is not None else None,
                    headers=headers,
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="walmart-unreachable",
                    status_code=502,
                    code="ERR_WALMART_UNREACHABLE",
                    title="Walmart Unreachable",
                    detail=(
                        f"Walmart Marketplace call '{method} {path}' failed before any "
                        "response arrived."
                    ),
                ) from None
        self._raise_for_transport_status(response)
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # ----------------------------------------------------------------------- OAuth2 token

    async def _access_token_or_refresh(self) -> str:
        """Return a cached access token, exchanging credentials only once it lapses."""
        if self._access_token is not None and time.monotonic() < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token is not None and time.monotonic() < self._token_expires_at:
                return self._access_token
            return await self._refresh_access_token()

    async def _refresh_access_token(self) -> str:
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}{WALMART_TOKEN_PATH}",
                    headers={
                        **self._base_headers(),
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="walmart-auth-failed",
                    status_code=502,
                    code="ERR_WALMART_AUTH_FAILED",
                    title="Walmart Authentication Failed",
                    detail="Walmart's OAuth2 token endpoint was unreachable.",
                ) from None
        self._raise_for_transport_status(response)
        if response.status_code >= 400:
            raise AppError(
                slug="walmart-auth-failed",
                status_code=502,
                code="ERR_WALMART_AUTH_FAILED",
                title="Walmart Authentication Failed",
                detail=(
                    "Walmart refused the client-credentials exchange "
                    f"(HTTP {response.status_code})."
                ),
            )
        payload = self._json_object(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise AppError(
                slug="walmart-auth-failed",
                status_code=502,
                code="ERR_WALMART_AUTH_FAILED",
                title="Walmart Authentication Failed",
                detail="Walmart returned a token response without a usable access_token.",
            )
        ttl = self._as_int(payload.get("expires_in"), WALMART_DEFAULT_TOKEN_TTL_SECONDS)
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(
            float(ttl) - WALMART_TOKEN_EXPIRY_BUFFER_SECONDS, 0.0
        )
        logger.info(
            "walmart_access_token_refreshed",
            seller_id=self.seller_id,
            fulfillment_mode=self.fulfillment_mode,
            expires_in=ttl,
        )
        return token

    # ---------------------------------------------------------------------- value coercion

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        if isinstance(value, bool) or value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_decimal(value: Any) -> Decimal:
        """Parse a Walmart money value into 2dp ``Decimal``; never fall back to ``float``."""
        if value is None or value == "":
            return Decimal("0.00").quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        if isinstance(value, bool):
            raise AppError(
                slug="walmart-invalid-money",
                status_code=502,
                code="ERR_WALMART_INVALID_MONEY",
                title="Invalid Walmart Money Value",
                detail="Walmart returned a boolean where a monetary amount was expected.",
            )
        try:
            return Decimal(str(value)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        except (ArithmeticError, ValueError):
            raise AppError(
                slug="walmart-invalid-money",
                status_code=502,
                code="ERR_WALMART_INVALID_MONEY",
                title="Invalid Walmart Money Value",
                detail="Walmart returned a monetary value that is not a finite decimal number.",
            ) from None

    @classmethod
    def _money(cls, payload: Mapping[str, Any], key: str) -> Decimal:
        """Read ``{"key": {"amount": "12.34", "currency": "USD"}}`` or a bare amount."""
        amount = payload.get(key)
        if isinstance(amount, Mapping):
            return cls._as_decimal(amount.get("amount"))
        return cls._as_decimal(amount)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        """Walmart's filter form: ``2024-05-01T00:00:00.000Z`` (UTC)."""
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

    @staticmethod
    def _parse_offset(cursor: str | None) -> int:
        if cursor is None or not str(cursor).strip():
            return 0
        try:
            return max(int(str(cursor).strip()), 0)
        except ValueError:
            raise AppError(
                slug="walmart-invalid-cursor",
                status_code=422,
                code="ERR_WALMART_INVALID_CURSOR",
                title="Invalid Walmart Cursor",
                detail="Walmart offset cursors are non-negative integers.",
            ) from None

    @staticmethod
    def _next_offset_cursor(*, offset: int, fetched: int, total: Any) -> str | None:
        """Advance the offset cursor while Walmart reports more rows than we just read."""
        if fetched <= 0:
            return None
        advanced = offset + fetched
        total_count = WalmartAdapter._as_int(total, -1)
        if total_count < 0:
            return str(advanced) if fetched >= WALMART_PAGE_LIMIT else None
        return str(advanced) if total_count > advanced else None

    # -------------------------------------------------------------------- normalization

    def _line_charge_totals(
        self, row: Mapping[str, Any]
    ) -> tuple[Decimal, Decimal, Decimal, str | None]:
        """Split one Walmart order line's charges into ``(product, shipping, tax, currency)``."""
        charge_rows = _mapping(row.get("charges")).get("charge")
        rows = charge_rows if isinstance(charge_rows, list) else []
        product = shipping = tax = Decimal("0.00")
        currency: str | None = None
        for charge in rows:
            if not isinstance(charge, Mapping):
                continue
            amount_payload = _mapping(charge.get("chargeAmount"))
            amount = self._as_decimal(amount_payload.get("amount"))
            declared = str(amount_payload.get("currency") or "").strip().upper()
            if declared:
                currency = declared
            charge_type = str(charge.get("chargeType") or "").strip().upper()
            if charge_type == "PRODUCT":
                product += amount
            elif charge_type == "SHIPPING":
                shipping += amount
            elif charge_type == "TAX":
                tax += amount
        return product, shipping, tax, currency

    def _line_status(self, row: Mapping[str, Any]) -> FulfillmentStatus:
        """Resolve one order line's current status (Walmart appends history oldest-first)."""
        status_rows = _mapping(row.get("orderLineStatuses")).get("orderLineStatus")
        rows = status_rows if isinstance(status_rows, list) else []
        for status_row in reversed(rows):
            current = _mapping(_mapping(status_row).get("currentStatus"))
            declared = str(current.get("status") or "").strip().upper()
            resolved = _ORDER_LINE_STATUS_MAP.get(declared)
            if resolved is not None:
                return resolved
        return FulfillmentStatus.CONFIRMED_RESERVED

    @staticmethod
    def _rollup_fulfillment(statuses: Sequence[FulfillmentStatus]) -> FulfillmentStatus:
        """Roll per-line statuses up into the one status the ERP order carries."""
        if not statuses:
            return FulfillmentStatus.CONFIRMED_RESERVED
        live = [status for status in statuses if status not in _DEAD_LINE]
        if not live:
            return (
                FulfillmentStatus.REFUNDED
                if FulfillmentStatus.REFUNDED in set(statuses)
                else FulfillmentStatus.CANCELLED
            )
        if all(status is FulfillmentStatus.DELIVERED for status in live):
            return FulfillmentStatus.DELIVERED
        if all(status in _SHIPPED_LIKE for status in live):
            return FulfillmentStatus.SHIPPED
        if any(status in _SHIPPED_LIKE for status in live):
            return FulfillmentStatus.PARTIALLY_SHIPPED
        return FulfillmentStatus.CONFIRMED_RESERVED

    def _line_refund_amount(self, row: Mapping[str, Any]) -> Decimal:
        """Sum any explicit per-line ``refund.refundCharges`` amounts (2dp, positive magnitude)."""
        refund_block = _mapping(row.get("refund"))
        raw_charges = refund_block.get("refundCharges")
        charge_rows = (
            raw_charges.get("refundCharge") if isinstance(raw_charges, Mapping) else raw_charges
        )
        if not isinstance(charge_rows, list):
            return Decimal("0.00")
        total_refund = Decimal("0.00")
        for entry in charge_rows:
            if not isinstance(entry, Mapping):
                continue
            charge = (
                _mapping(entry.get("charge"))
                if isinstance(entry.get("charge"), Mapping)
                else entry
            )
            amount_payload = _mapping(charge.get("chargeAmount"))
            if amount_payload.get("amount") is not None:
                total_refund += self._as_decimal(amount_payload.get("amount")).copy_abs()
        return total_refund.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)

    @staticmethod
    def _payment_status(order: Mapping[str, Any], *, dead: bool) -> PaymentStatus:
        """Walmart collects payment up front; only an explicit negative signal downgrades it."""
        if dead:
            return PaymentStatus.REFUNDED
        declared = str(order.get("paymentStatus") or "").strip().upper()
        if declared in {"UNPAID", "PENDING", "FAILED"}:
            return PaymentStatus.UNPAID
        if declared == "PARTIALLY_PAID":
            return PaymentStatus.PARTIALLY_PAID
        if declared in {"REFUNDED", "FULLY_REFUNDED"}:
            return PaymentStatus.REFUNDED
        return PaymentStatus.PAID

    def _normalize_order(self, order: Mapping[str, Any]) -> NormalizedOrder | None:
        """Normalize one Walmart purchase order, or ``None`` when it is unusable as-is."""
        external_order_id = str(order.get("purchaseOrderId") or "").strip()
        if not external_order_id:
            return None
        raw_lines = order.get("orderLines")
        line_rows = raw_lines.get("orderLine") if isinstance(raw_lines, Mapping) else raw_lines
        rows = line_rows if isinstance(line_rows, list) else []

        # Walmart can repeat a SKU across lines (split fulfilment or re-priced lines); the ERP
        # order item is per variant, so merge by SKU and keep the feed's original ordering.
        merged: dict[str, NormalizedLineItem] = {}
        order_currencies: set[str] = set()
        refunded = Decimal("0.00")
        subtotal = Decimal("0.00")
        shipping = Decimal("0.00")
        tax = Decimal("0.00")
        line_statuses: list[FulfillmentStatus] = []

        for row in rows:
            if not isinstance(row, Mapping):
                continue
            item = _mapping(row.get("item"))
            sku = str(item.get("sku") or row.get("itemSku") or "").strip()
            if not sku:
                continue
            quantity = max(
                self._as_int(_mapping(row.get("orderLineQuantity")).get("amount"), 1), 1
            )
            product, line_shipping, line_tax, currency = self._line_charge_totals(row)
            if currency:
                order_currencies.add(currency)
            subtotal += product
            shipping += line_shipping
            tax += line_tax
            status = self._line_status(row)
            line_statuses.append(status)
            line_refund = self._line_refund_amount(row)
            if line_refund > Decimal("0"):
                refunded += line_refund
            elif status in _DEAD_LINE:
                refunded += product
            title = str(item.get("productName") or sku)
            existing = merged.get(sku)
            if existing is None:
                merged[sku] = NormalizedLineItem(
                    external_sku=sku,
                    title=title,
                    quantity=quantity,
                    line_total=product,
                )
            else:
                existing.quantity += quantity
                existing.line_total += product

        line_items = list(merged.values())
        if not line_items:
            return None
        if len(order_currencies) > 1:
            raise AppError(
                slug="walmart-mixed-order-currency",
                status_code=502,
                code="ERR_WALMART_MIXED_ORDER_CURRENCY",
                title="Mixed Walmart Order Currency",
                detail=(
                    f"Walmart purchase order '{external_order_id}' mixes "
                    f"{', '.join(sorted(order_currencies))}; refusing to book a single "
                    "order currency."
                ),
            )
        currency = next(iter(order_currencies), "USD")
        fulfillment_status = self._rollup_fulfillment(line_statuses)
        total = (subtotal + shipping + tax).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        declared_payment = str(order.get("paymentStatus") or "").strip().upper()
        is_full_refund = (
            declared_payment in {"REFUNDED", "FULLY_REFUNDED"}
            or (total > Decimal("0") and refunded >= total)
        )
        if fulfillment_status is not FulfillmentStatus.CANCELLED and is_full_refund:
            fulfillment_status = FulfillmentStatus.REFUNDED
            if refunded == Decimal("0") and total > Decimal("0"):
                refunded = total
        elif (
            fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
            and declared_payment in {"UNPAID", "PENDING", "FAILED"}
        ):
            fulfillment_status = FulfillmentStatus.PENDING_PAYMENT
        dead = fulfillment_status in _DEAD_LINE

        # Walmart reports commissions in the settlement report, not the order feed: pay the
        # figure through only when the platform actually sent one instead of inventing a rate.
        commission = self._money(order, "commission")
        if order.get("totalCommission") is not None:
            commission = self._money(order, "totalCommission")

        shipping_info = _mapping(order.get("shippingInfo"))
        address = _mapping(shipping_info.get("postalAddress"))
        customer_payload = _mapping(order.get("customer"))
        email = (
            str(
                order.get("customerEmailId")
                or customer_payload.get("emailId")
                or address.get("email")
                or ""
            ).strip()
            or None
        )
        phone_e164 = str(address.get("phone") or "").strip() or None
        country_code = str(address.get("country") or "").strip().upper() or None
        display_name = (
            str(address.get("name") or "").strip()
            or (email or "")
            or "Walmart Buyer"
        )

        metadata: dict[str, Any] = {
            "walmart_purchase_order_id": external_order_id,
            "walmart_customer_order_id": order.get("customerOrderId"),
            "walmart_seller_id": self.seller_id,
            "walmart_fulfillment_mode": self.fulfillment_mode,
            "walmart_order_type": order.get("orderType"),
            "walmart_ship_node": order.get("shipNode"),
            "walmart_payment_status": order.get("paymentStatus"),
        }
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )
        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=str(order.get("customerOrderId") or external_order_id),
            order_mode=OrderMode.B2C_RETAIL,
            currency=currency,
            fx_rate_to_base=None,
            subtotal_amount=subtotal.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP),
            shipping_charged=shipping.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP),
            discount_amount=Decimal("0.00"),
            tax_collected=tax.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP),
            total_amount=total,
            platform_commission_base=commission.quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=self._payment_status(order, dead=dead),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone_e164,
                external_customer_id=email or phone_e164,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=display_name,
                country_code=country_code,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=self._parse_datetime(
                order.get("lastModifiedDate") or order.get("orderDate")
            ),
        )

    def _normalize_product(self, row: Mapping[str, Any]) -> NormalizedProduct | None:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            return None
        title = str(row.get("productName") or sku)
        price = _mapping(row.get("price"))
        price_amount = price.get("amount")
        price_currency = str(price.get("currency") or "").strip().upper() or None
        availability = _mapping(row.get("availability"))
        published_status = row.get("publishedStatus")
        return NormalizedProduct(
            external_product_id=sku,
            title=title,
            variants=[
                {
                    "sku": sku,
                    "title": title,
                    "price": (
                        str(self._as_decimal(price_amount))
                        if price_amount is not None
                        else None
                    ),
                    "currency": price_currency,
                    "published_status": published_status,
                    "lifecycle_status": row.get("lifecycleStatus"),
                    "variant_group_id": row.get("variantGroupId"),
                    "gtin": row.get("gtin") or row.get("upc") or row.get("ean"),
                    "available_quantity": (
                        self._as_int(availability.get("amount"), 0)
                        if availability.get("amount") is not None
                        else None
                    ),
                }
            ],
        )

    # ---------------------------------------------------------------- CommercePlatformAdapter

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        """Pull the ``lastModifiedStartDate`` order feed, following Walmart's nextCursor."""
        params: dict[str, Any] = {
            "lastModifiedStartDate": self._format_datetime(since),
            "limit": WALMART_PAGE_LIMIT,
        }
        if cursor is not None and str(cursor).strip():
            params["nextCursor"] = str(cursor).strip()
        response = await self._request("GET", WALMART_ORDERS_PATH, params=params)
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="walmart-order-sync-failed",
                code="ERR_WALMART_ORDER_SYNC_FAILED",
                title="Walmart Order Sync Failed",
            )
        payload = self._json_object(response)
        list_payload = _mapping(payload.get("list"))
        elements_payload = _mapping(list_payload.get("elements"))
        raw_rows = elements_payload.get("order")
        rows = raw_rows if isinstance(raw_rows, list) else []
        orders = [
            normalised
            for row in rows
            if isinstance(row, Mapping) and (normalised := self._normalize_order(row)) is not None
        ]
        next_cursor = str(_mapping(list_payload.get("meta")).get("nextCursor") or "").strip()
        return orders, (next_cursor or None)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        """Walk the Walmart item feed; the API exposes no modified-since filter."""
        del since
        offset = self._parse_offset(cursor)
        response = await self._request(
            "GET",
            WALMART_ITEMS_PATH,
            params={"limit": WALMART_PAGE_LIMIT, "offset": offset},
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="walmart-product-sync-failed",
                code="ERR_WALMART_PRODUCT_SYNC_FAILED",
                title="Walmart Product Sync Failed",
            )
        payload = self._json_object(response)
        raw_rows = payload.get("ItemResponse")
        rows = raw_rows if isinstance(raw_rows, list) else []
        products: list[NormalizedProduct] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            product = self._normalize_product(row)
            if product is not None:
                products.append(product)
        return products, self._next_offset_cursor(
            offset=offset, fetched=len(rows), total=payload.get("totalItems")
        )

    @staticmethod
    def _carrier_payload(carrier_company: str) -> dict[str, str]:
        """Map a carrier name onto Walmart's ``carrierName`` enum plus its free-text twin."""
        cleaned = " ".join(carrier_company.split())
        code = _KNOWN_CARRIERS.get(cleaned.upper(), "Other")
        return {"carrier": code, "otherCarrier": cleaned}

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Mark every line of a Walmart purchase order shipped with this tracking number.

        Walmart refuses a shipment that does not name the ``lineNumber`` values it claims, so
        the purchase order is read first and its lines are shipped in one call.
        """
        del notify_customer  # Walmart notifies the buyer itself on the shipping update.
        clean_order_id = external_order_id.strip()
        clean_tracking = tracking_number.strip()
        if not clean_order_id:
            raise AppError(
                slug="walmart-fulfillment-rejected",
                status_code=422,
                code="ERR_WALMART_FULFILLMENT_REJECTED",
                title="Walmart Fulfillment Rejected",
                detail="A Walmart fulfillment requires a non-empty purchase order id.",
            )
        if not clean_tracking:
            raise AppError(
                slug="walmart-tracking-number-required",
                status_code=422,
                code="ERR_WALMART_TRACKING_NUMBER_REQUIRED",
                title="Walmart Tracking Number Required",
                detail="Walmart refuses a shipping update without a tracking number.",
            )
        order_path = f"{WALMART_ORDERS_PATH}/{quote(clean_order_id, safe='')}"
        order_response = await self._request("GET", order_path)
        if order_response.status_code >= 400:
            self._raise_api_error(
                order_response,
                slug="walmart-fulfillment-rejected",
                code="ERR_WALMART_FULFILLMENT_REJECTED",
                title="Walmart Fulfillment Rejected",
            )
        order_payload = self._json_object(order_response)
        raw_lines = _mapping(order_payload.get("orderLines")).get("orderLine")
        rows = [
            row
            for row in (raw_lines if isinstance(raw_lines, list) else [])
            if isinstance(row, Mapping) and str(row.get("lineNumber") or "").strip()
        ]
        shippable_rows = [
            row
            for row in rows
            if self._line_status(row)
            not in {
                FulfillmentStatus.CANCELLED,
                FulfillmentStatus.REFUNDED,
                FulfillmentStatus.SHIPPED,
                FulfillmentStatus.DELIVERED,
            }
        ]
        target_rows = shippable_rows if shippable_rows else rows
        order_lines: list[dict[str, Any]] = []
        line_numbers: list[str] = []
        for row in target_rows:
            line_number = str(row.get("lineNumber") or "").strip()
            quantity = max(
                self._as_int(_mapping(row.get("orderLineQuantity")).get("amount"), 1), 1
            )
            line_numbers.append(line_number)
            order_lines.append(
                {
                    "lineNumber": line_number,
                    "orderLineStatuses": {
                        "orderLineStatus": [
                            {
                                "status": "Shipped",
                                "statusQuantity": {
                                    "unitOfMeasurement": WALMART_INVENTORY_UNIT,
                                    "amount": str(quantity),
                                },
                                "trackingInfo": {
                                    "shipDateTime": int(datetime.now(UTC).timestamp() * 1000),
                                    "carrierName": self._carrier_payload(carrier_company),
                                    "methodCode": "Standard",
                                    "trackingNumber": clean_tracking,
                                    "trackingURL": tracking_url or "",
                                },
                            }
                        ]
                    },
                }
            )
        if not order_lines:
            raise AppError(
                slug="walmart-fulfillment-rejected",
                status_code=502,
                code="ERR_WALMART_FULFILLMENT_REJECTED",
                title="Walmart Fulfillment Rejected",
                detail=(
                    f"Walmart purchase order '{clean_order_id}' reported no order lines to "
                    "ship."
                ),
            )

        ship_response = await self._request(
            "POST",
            f"{order_path}/shipping",
            json_body={"orderShipment": {"orderLines": {"orderLine": order_lines}}},
        )
        if ship_response.status_code >= 300:
            self._raise_api_error(
                ship_response,
                slug="walmart-fulfillment-rejected",
                code="ERR_WALMART_FULFILLMENT_REJECTED",
                title="Walmart Fulfillment Rejected",
            )
        logger.info(
            "walmart_fulfillment_created",
            seller_id=self.seller_id,
            purchase_order_id=clean_order_id,
            fulfillment_order_id=fulfillment_order_id,
            fulfillment_mode=self.fulfillment_mode,
            line_numbers=line_numbers,
        )
        po_id = str(order_payload.get("purchaseOrderId") or clean_order_id)
        raw_ship = self._json_object(ship_response)
        fid = str(
            raw_ship.get("fulfillment_id")
            or raw_ship.get("shipmentId")
            or raw_ship.get("id")
            or (fulfillment_order_id or "").strip()
            or f"{po_id}:{'-'.join(line_numbers)}"
        )
        return {
            "channel": "walmart",
            "fulfillment_id": fid,
            "external_order_id": clean_order_id,
            "purchase_order_id": po_id,
            "tracking_number": clean_tracking,
            "carrier_company": carrier_company,
            "tracking_url": tracking_url,
            "fulfillment_mode": self.fulfillment_mode,
            "line_numbers": line_numbers,
            "raw": raw_ship,
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Fail-closed constant-time HMAC-SHA256 check of a Walmart webhook signature."""
        return verify_walmart_webhook(raw_body, headers, secret or self._client_secret)

    # ------------------------------------------------------------------- listing publishing

    @staticmethod
    def _listing_images(images: Sequence[str], extra: str | None = None) -> list[str]:
        ordered: list[str] = []
        for candidate in ([extra] if extra else []) + list(images):
            url = str(candidate or "").strip()
            if url.startswith("https://") and url not in ordered:
                ordered.append(url)
        return ordered[:WALMART_MAX_IMAGES]

    def _shelf_payload(
        self,
        *,
        description: str,
        bullet_points: Sequence[str],
        images: Sequence[str],
        variant_image_url: str | None,
    ) -> dict[str, Any]:
        """Build the ``children.Shelf`` merchandising block for one feed item."""
        shelf: dict[str, Any] = {}
        bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
        cleaned_description = description.strip()
        if cleaned_description:
            shelf["longDescription"] = cleaned_description
        if bullets:
            shelf["shortDescription"] = bullets[0][:200]
            shelf["keyFeatures"] = {"keyFeature": bullets}
        resolved_images = self._listing_images(images, variant_image_url)
        if resolved_images:
            shelf["mainImageUrl"] = resolved_images[0]
            secondary = resolved_images[1:]
            if secondary:
                shelf["productSecondaryImageURL"] = {"productImageUrl": secondary}
        return shelf

    def _feed_items(self, payload: MarketplaceListingPayload) -> list[dict[str, Any]]:
        """One ``MPItem`` per variant (or a single item when the listing has no variants).

        ``payload.quantity`` is deliberately not written into the feed: Walmart keeps a
        listing's sellable quantity on the inventory API, which ``push_inventory_level``
        (or an inventory feed) owns, so carrying a stale copy here could oversell.
        """
        base_price = Decimal(payload.price_usd).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        variant_list = list(payload.variants)
        option_names = [option.name for option in payload.options if option.name]
        if not option_names and variant_list:
            option_names = sorted(
                {name for variant in variant_list for name in variant.option_values}
            )
        group_id = payload.external_sku if len(variant_list) > 1 else None

        items: list[dict[str, Any]] = []
        if not variant_list:
            shelf = self._shelf_payload(
                description=payload.description,
                bullet_points=payload.bullet_points,
                images=payload.images,
                variant_image_url=None,
            )
            orderable: dict[str, Any] = {
                "sku": payload.external_sku,
                "productName": payload.title[:WALMART_MAX_TITLE_LENGTH],
                "price": {"currency": WALMART_LISTING_CURRENCY, "amount": str(base_price)},
            }
            if shelf:
                orderable["children"] = {"Shelf": shelf}
            items.append({"Orderable": orderable})
            return items

        for index, variant in enumerate(variant_list):
            variant_price = Decimal(variant.price_usd).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
            shelf = self._shelf_payload(
                description=payload.description,
                bullet_points=payload.bullet_points,
                images=payload.images,
                variant_image_url=variant.image_url,
            )
            orderable = {
                "sku": variant.external_sku or payload.external_sku,
                "productName": payload.title[:WALMART_MAX_TITLE_LENGTH],
                "price": {
                    "currency": WALMART_LISTING_CURRENCY,
                    "amount": str(variant_price),
                },
                "variantGroupId": group_id or payload.external_sku,
                "variantAttributeNames": {"variantAttributeName": option_names},
                "isPrimaryVariant": "Yes" if index == 0 else "No",
            }
            if shelf:
                orderable["children"] = {"Shelf": shelf}
            items.append({"Orderable": orderable})
        return items

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Submit an ``MP_ITEM`` feed — Walmart creates the listing asynchronously.

        The receipt is therefore ``submitted``, not ``published``, and carries Walmart's
        ``feedId``; the feed result poll is the caller's next step. Binary
        ``payload.image_blobs`` are not uploaded: Walmart ingests listing media through a
        separate Media API, and pretending a byte upload happened would be a lie.
        """
        cleaned_sku = payload.external_sku.strip()
        cleaned_title = payload.title.strip()
        if not cleaned_sku or not cleaned_title:
            raise AppError(
                slug="walmart-listing-rejected",
                status_code=422,
                code="ERR_WALMART_LISTING_REJECTED",
                title="Walmart Listing Rejected",
                detail="A Walmart listing requires a non-empty SKU and title.",
            )
        if isinstance(payload.price_usd, float):  # pragma: no cover - DTO is Decimal-typed
            raise AppError(
                slug="walmart-invalid-money",
                status_code=422,
                code="ERR_WALMART_INVALID_MONEY",
                title="Invalid Walmart Money Value",
                detail=(
                    "Walmart listing prices must be Decimal; IEEE-754 float prices are "
                    "refused so no binary rounding error can reach a live listing."
                ),
            )
        feed_items = self._feed_items(payload)
        feed_body: dict[str, Any] = {
            "MPItemFeedHeader": {
                "version": WALMART_FEED_VERSION,
                "feedType": WALMART_FEED_TYPE,
                "locale": WALMART_FEED_LOCALE,
            },
            "MPItem": feed_items,
        }
        response = await self._request(
            "POST",
            WALMART_FEEDS_PATH,
            params={"feedType": WALMART_FEED_TYPE},
            json_body=feed_body,
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="walmart-listing-rejected",
                code="ERR_WALMART_LISTING_REJECTED",
                title="Walmart Listing Rejected",
            )
        response_payload = self._json_object(response)
        feed_id = str(response_payload.get("feedId") or "").strip()
        if not feed_id:
            raise AppError(
                slug="walmart-listing-rejected",
                status_code=502,
                code="ERR_WALMART_LISTING_REJECTED",
                title="Walmart Listing Rejected",
                detail=(
                    f"Walmart accepted the MP_ITEM feed for '{cleaned_sku}' without returning "
                    "a feedId."
                ),
            )
        logger.info(
            "walmart_listing_submitted",
            seller_id=self.seller_id,
            external_sku=cleaned_sku,
            feed_id=feed_id,
            variants=len(payload.variants),
        )
        return MarketplaceListingDispatchReceipt(
            # Walmart identifies an item by its seller SKU; the feed id is not a product id.
            external_product_id=cleaned_sku,
            external_sku=cleaned_sku,
            status="submitted",
            variant_receipts=[
                MarketplaceVariantReceipt(
                    external_sku=str(item["Orderable"].get("sku") or cleaned_sku),
                    external_variant_id=None,
                    external_inventory_item_id=None,
                )
                for item in feed_items
            ],
            raw_response={
                "feedId": feed_id,
                "feedType": WALMART_FEED_TYPE,
                "itemCount": len(feed_items),
                "response": response_payload,
            },
        )

    # --------------------------------------------------------------------- inventory sync

    async def push_inventory_level(
        self,
        *,
        external_sku: str,
        quantity: int,
        external_product_id: str | None = None,
        external_variant_id: str | None = None,
        external_inventory_item_id: str | None = None,
        location_id: str | None = None,
        warehouse_id: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        """Push one SKU's sellable quantity so Walmart never oversells ERP stock."""
        del external_product_id, external_variant_id
        cleaned_sku = str(external_sku).strip()
        if not cleaned_sku:
            raise AppError(
                slug="walmart-inventory-sync-failed",
                status_code=422,
                code="ERR_WALMART_INVENTORY_SYNC",
                title="Walmart Inventory Sync Failed",
                detail="Walmart inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, self._as_int(quantity, 0))
        response = await self._request(
            "PUT",
            WALMART_INVENTORY_PATH,
            params={"sku": cleaned_sku},
            json_body={
                "sku": cleaned_sku,
                "quantity": {"unit": WALMART_INVENTORY_UNIT, "amount": norm_qty},
            },
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="walmart-inventory-sync-failed",
                code="ERR_WALMART_INVENTORY_SYNC",
                title="Walmart Inventory Sync Failed",
            )
        logger.info(
            "walmart_inventory_pushed",
            seller_id=self.seller_id,
            external_sku=cleaned_sku,
            quantity=norm_qty,
            location_id=(location_id or "").strip() or None,
            warehouse_id=(warehouse_id or "").strip() or None,
        )
        return {
            "channel": "walmart",
            "external_sku": cleaned_sku,
            "quantity": norm_qty,
            "external_inventory_item_id": (external_inventory_item_id or "").strip() or None,
            "location_id": (location_id or "").strip() or None,
            "warehouse_id": (warehouse_id or "").strip() or None,
            "raw": self._json_object(response),
        }


__all__ = [
    "WALMART_DEFAULT_TOKEN_TTL_SECONDS",
    "WALMART_FEEDS_PATH",
    "WALMART_INVENTORY_PATH",
    "WALMART_ITEMS_PATH",
    "WALMART_ORDERS_PATH",
    "WALMART_PAGE_LIMIT",
    "WALMART_PRODUCTION_BASE_URL",
    "WALMART_SANDBOX_BASE_URL",
    "WALMART_TOKEN_PATH",
    "WalmartAdapter",
    "verify_walmart_webhook",
]
