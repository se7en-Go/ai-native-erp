"""Lazada Open Platform connector: signed REST calls, order/product sync, live publish.

Signing follows the Lazada Open Platform canonical form: every request parameter
(``app_key``, ``timestamp``, ``sign_method``, ``access_token`` and the endpoint's own
parameters, ``sign`` excluded) is flattened to ``key + value`` in ascending key order and
hashed with HMAC-SHA256 using the app secret, emitted as upper-case hex. The secret never
travels on the wire, never reaches a log line and is scrubbed from every :class:`AppError`
detail. Monetary math is always :class:`decimal.Decimal` with ``ROUND_HALF_UP``; no binary
float ever touches a price, a commission or a refund.

Webhook pushes are verified fail-closed: only a constant-time HMAC-SHA256 over the raw body
matching ``x-lazada-signature`` (or the ``authorization`` header) is accepted. A bare
app-secret in ``authorization`` (the legacy Lazada push style) is deliberately rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

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

#: Lazada runs one host per seller region; the API root is ``https://api.lazada.<tld>/rest``.
LAZADA_REGION_DOMAINS: Mapping[str, str] = {
    "SG": "sg",
    "MY": "my",
    "TH": "th",
    "ID": "id",
    "PH": "ph",
    "VN": "vn",
}
LAZADA_DEFAULT_REGION = "SG"
LAZADA_DEFAULT_TIMEOUT_SECONDS = 20.0
LAZADA_SIGNATURE_HEADER = "x-lazada-signature"
LAZADA_AUTHORIZATION_HEADER = "authorization"

LAZADA_ORDERS_PATH = "/orders/get"
LAZADA_ORDER_ITEMS_PATH = "/order/items/get"
LAZADA_ORDERS_FULFILL_PATH = "/order/fulfill"
LAZADA_PRODUCTS_PATH = "/products/get"
LAZADA_PRODUCT_CREATE_PATH = "/product/create"
LAZADA_PRICE_QUANTITY_PATH = "/product/price_quantity/update"

#: Lazada's list endpoints accept at most 100 rows; 50 keeps each sync page cheap.
LAZADA_PAGE_SIZE = 50
#: All money lands in NUMERIC(…, 2) columns; quantize once, half-up, never from a float.
MONEY_SCALE = Decimal("0.01")
#: Lazada requires a numeric ``PrimaryCategory``; text categories fall back to this one.
DEFAULT_LAZADA_PRIMARY_CATEGORY = "10000348"
#: ``fulfillment_mode`` -> Lazada ``delivery_type`` token.
LAZADA_DELIVERY_TYPE_MAP: Mapping[str, str] = {
    "standard": "dropship",
    "dropship": "dropship",
    "dropshipping": "dropship",
    "pickup": "pickup",
    "self_delivery": "send_delivery",
    "send_delivery": "send_delivery",
}
_UNSIGNED_PARAMS = frozenset({"sign"})
_SUCCESS_CODES = frozenset({"0", "200", "success", "true"})
_CANCELLED_STATUSES = frozenset({"canceled", "cancelled"})

ORDER_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "pending": FulfillmentStatus.PENDING_PAYMENT,
    "processing": FulfillmentStatus.CONFIRMED_RESERVED,
    "ready_to_ship": FulfillmentStatus.CONFIRMED_RESERVED,
    "ready_to_pick": FulfillmentStatus.CONFIRMED_RESERVED,
    "shipped": FulfillmentStatus.SHIPPED,
    "delivered": FulfillmentStatus.DELIVERED,
    "returned": FulfillmentStatus.REFUNDED,
    "canceled": FulfillmentStatus.CANCELLED,
    "cancelled": FulfillmentStatus.CANCELLED,
    "failed": FulfillmentStatus.EXCEPTION,
    "lost": FulfillmentStatus.EXCEPTION,
    "damaged": FulfillmentStatus.EXCEPTION,
}

_ORDER_ROW_KEYS = ("orders", "order_list")
_PRODUCT_ROW_KEYS = ("products", "product_list")
_ORDER_ITEM_KEYS = ("order_items", "order_item_list")


def _param_text(value: Any) -> str:
    """Flatten a parameter value into the exact string that gets signed and sent."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    return str(value)


def sign_lazada_request(params: Mapping[str, Any], app_secret: str) -> str:
    """Canonical Lazada signature: ``HmacSHA256(secret, sorted(k + v))`` as upper-case hex.

    ``sign`` is skipped so the helper is idempotent on an already-signed parameter map,
    which is what lets a consumer re-verify a request byte for byte.
    """
    if not app_secret:
        raise ValueError("app_secret is required to sign Lazada Open Platform requests")
    canonical = "".join(
        f"{key}{_param_text(params[key])}" for key in sorted(params) if key not in _UNSIGNED_PARAMS
    )
    return (
        hmac.new(app_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256)
        .hexdigest()
        .upper()
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (Lazada gateways mix header casing)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted and value:
            return str(value)
    return None


def verify_lazada_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    """Fail-closed, constant-time verification of the Lazada push signature.

    Accepts the hex (either case) or base64 form of ``HMAC-SHA256(secret, raw_body)`` in
    ``x-lazada-signature``, falling back to ``authorization``. An empty secret, a missing
    header or a non-ASCII signature all fail closed instead of raising.
    """
    if not secret:
        return False
    received = _header(headers, LAZADA_SIGNATURE_HEADER) or _header(
        headers, LAZADA_AUTHORIZATION_HEADER
    )
    if not received:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    candidates = (
        digest.hexdigest(),
        digest.hexdigest().upper(),
        base64.b64encode(digest.digest()).decode("ascii"),
    )
    token = received.strip()
    try:
        return any(hmac.compare_digest(candidate, token) for candidate in candidates)
    except (TypeError, ValueError):
        return False


def _lookup(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _first_str(source: Mapping[str, Any], *keys: str) -> str | None:
    value = _lookup(source, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_decimal(value: Any, *, what: str) -> Decimal:
    """Parse a Lazada numeric field without letting a binary float into money math."""
    if value is None or isinstance(value, bool):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise AppError(
            slug="lazada-api-error",
            status_code=502,
            code="ERR_LAZADA_API_ERROR",
            title="Lazada API Error",
            detail=f"Lazada response carried an unparseable '{what}' value.",
        ) from exc


def _money(value: Any, *, what: str = "amount") -> Decimal:
    return _to_decimal(value, what=what).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _sum_money(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal("0.00")).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _to_int(value: Any, *, what: str) -> int:
    return int(_to_decimal(value, what=what))


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _lazada_timestamp(value: datetime) -> str:
    """Render the ``yyyy-MM-ddTHH:mm:ss+00:00`` window bound Lazada expects."""
    return _as_utc(value).isoformat(timespec="seconds")


def _lazada_datetime(value: Any, fallback: datetime) -> datetime:
    """Parse a Lazada timestamp (ISO-8601, ``Y-m-d H:M:S`` or epoch millis) as UTC."""
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if abs(value) > 100_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if not text:
        return fallback
    if text.isdigit():
        epoch = int(text)
        return datetime.fromtimestamp(epoch / 1000 if epoch > 100_000_000_000 else epoch, UTC)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return fallback
    return _as_utc(parsed)


def _offset(cursor: str | None) -> int:
    """Lazada list endpoints page by a 0-based row offset, echoed back as the cursor."""
    if cursor is None or not str(cursor).strip():
        return 0
    text = str(cursor).strip()
    if not text.isdigit():
        raise AppError(
            slug="lazada-invalid-cursor",
            status_code=422,
            code="ERR_LAZADA_INVALID_CURSOR",
            title="Invalid Lazada Cursor",
            detail="Lazada cursors are non-negative row offsets.",
        )
    return int(text)


def _listing_description(description: str, bullet_points: Sequence[str]) -> str:
    body = str(description).strip()
    bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
    if not bullets:
        return body
    bullet_block = "\n".join(f"- {bullet}" for bullet in bullets)
    return "\n\n".join(part for part in (body, bullet_block) if part)


def _https_images(urls: Iterable[str | None]) -> list[str]:
    """Keep only absolute https image URLs; Lazada rejects anything else at publish time."""
    cleaned: list[str] = []
    for url in urls:
        text = str(url or "").strip()
        if text.startswith("https://") and text not in cleaned:
            cleaned.append(text)
    return cleaned


class LazadaAdapter(CommercePlatformAdapter):
    """Lazada Open Platform adapter for one seller (region-scoped, BYOK credentials)."""

    def __init__(
        self,
        *,
        seller_id: str,
        app_key: str,
        app_secret: str,
        access_token: str,
        region: str = LAZADA_DEFAULT_REGION,
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = LAZADA_DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("seller_id", seller_id),
                ("app_key", app_key),
                ("app_secret", app_secret),
                ("access_token", access_token),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} are required for the Lazada Open Platform adapter"
            )
        region_code = str(region or LAZADA_DEFAULT_REGION).strip().upper()
        domain = LAZADA_REGION_DOMAINS.get(region_code)
        custom_endpoint = str(endpoint_url or "").strip()
        if domain is None and not custom_endpoint:
            raise ValueError(
                f"Unsupported Lazada region '{region}'; expected one of "
                f"{', '.join(sorted(LAZADA_REGION_DOMAINS))} or an explicit endpoint_url."
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero for the Lazada adapter")

        self.seller_id = seller_id.strip()
        self.app_key = app_key.strip()
        self._app_secret = app_secret
        self._access_token = access_token
        self.region = region_code
        self.fulfillment_mode = str(fulfillment_mode or "standard").strip().lower() or "standard"
        self.endpoint_url = (custom_endpoint or f"https://api.lazada.{domain}/rest").rstrip("/")
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return (
            f"LazadaAdapter(seller_id={self.seller_id!r}, region={self.region!r}, "
            f"fulfillment_mode={self.fulfillment_mode!r}, "
            f"endpoint_url={self.endpoint_url!r}, app_key='[REDACTED]', "
            "app_secret='[REDACTED]', access_token='[REDACTED]')"
        )

    # ------------------------------------------------------------------ transport

    def _redact(self, text: str) -> str:
        """Strip every credential the adapter holds out of anything user-visible."""
        cleaned = text
        for secret in (self._app_secret, self._access_token):
            if secret:
                cleaned = cleaned.replace(secret, "[REDACTED]")
        return cleaned

    @staticmethod
    def _parse_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    def _raise_for_failure(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        raw_text: str,
        *,
        method: str,
        path: str,
    ) -> None:
        code = _first_str(payload, "code", "error_code") or ""
        message = _first_str(payload, "message", "msg", "error_msg", "detail") or ""
        failed = (
            status_code >= 400
            or (code and code.strip().lower() not in _SUCCESS_CODES)
            or payload.get("success") is False
        )
        if not failed:
            return

        haystack = f"{code} {message}".lower()
        auth_failed = status_code in {401, 403} or any(
            token in haystack
            for token in (
                "invalidsignature",
                "signature",
                "invalidaccesstoken",
                "illegalaccesstoken",
                "accesstoken",
                "access token",
                "unauthorized",
                "forbidden",
                "permission",
            )
        )
        rate_limited = status_code == 429 or any(
            token in haystack for token in ("throttl", "rate limit", "ratelimit", "flow control")
        )
        reason = message or raw_text.strip()[:200] or "empty response body"
        detail = self._redact(
            f"Lazada {method} {path} failed (http_status={status_code}, "
            f"code={code or 'unknown'}): {reason}"
        )[:500]
        logger.warning(
            "lazada_request_failed",
            seller_id=self.seller_id,
            region=self.region,
            path=path,
            http_status=status_code,
            lazada_code=code,
            auth_failed=auth_failed,
            rate_limited=rate_limited,
        )
        if auth_failed:
            raise AppError(
                slug="lazada-auth-failed",
                status_code=502,
                code="ERR_LAZADA_AUTH_FAILED",
                title="Lazada Authentication Failed",
                detail=detail,
            )
        if rate_limited:
            raise AppError(
                slug="lazada-rate-limited",
                status_code=429,
                code="ERR_LAZADA_RATE_LIMITED",
                title="Lazada Rate Limited",
                detail=detail,
            )
        raise AppError(
            slug="lazada-api-error",
            status_code=502,
            code="ERR_LAZADA_API_ERROR",
            title="Lazada API Error",
            detail=detail,
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sign and execute one Lazada call, mapping the envelope to typed ``AppError``s.

        The flat parameter map (system params plus the endpoint's own scalar parameters) is
        what gets signed; a nested business payload rides in the JSON body so the signature
        covers exactly the bytes the gateway re-computes.
        """
        signed: dict[str, str] = {
            "app_key": self.app_key,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
            "access_token": self._access_token,
        }
        for key, value in (params or {}).items():
            if key in _UNSIGNED_PARAMS:
                continue
            signed[key] = _param_text(value)
        signed["sign"] = sign_lazada_request(signed, self._app_secret)

        upper_method = method.upper()
        content = json.dumps(body, separators=(",", ":"), default=str) if body is not None else None
        headers = {"Accept": "application/json"}
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.request(
                    upper_method,
                    f"{self.endpoint_url}{path}",
                    params=signed,
                    content=content,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "lazada_transport_error",
                seller_id=self.seller_id,
                region=self.region,
                path=path,
                method=upper_method,
                error=self._redact(str(exc))[:300],
            )
            raise AppError(
                slug="lazada-api-error",
                status_code=502,
                code="ERR_LAZADA_API_ERROR",
                title="Lazada API Error",
                detail=self._redact(
                    f"Lazada {upper_method} {path} failed before a response was received; "
                    "the request can be retried."
                ),
            ) from exc

        payload = self._parse_payload(response)
        self._raise_for_failure(
            response.status_code,
            payload,
            self._redact(response.text),
            method=upper_method,
            path=path,
        )
        return payload

    # -------------------------------------------------------------- payload helpers

    @staticmethod
    def _data_of(payload: Mapping[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, Mapping):
            return dict(data)
        return {}

    @staticmethod
    def _rows(source: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
        for key in keys:
            raw = source.get(key)
            if isinstance(raw, list):
                return [row for row in raw if isinstance(row, Mapping)]
        return []

    # ---------------------------------------------------------------- normalization

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Build one line item from an explicit total or ``unit price * quantity``.

        Lazada's ``item_price`` is per unit; using it as the line total would silently
        under-report multi-unit lines (and therefore revenue and CVP).
        """
        sku = _first_str(item, "shop_sku", "seller_sku", "sku", "SkuId")
        if sku is None:
            logger.warning(
                "lazada_line_item_missing_sku",
                seller_id=self.seller_id,
                order_id=_first_str(item, "order_id"),
                order_item_id=_first_str(item, "order_item_id"),
            )
            return None
        quantity = max(1, _to_int(_lookup(item, "quantity", "qty"), what="quantity"))
        unit_price = _to_decimal(
            _lookup(item, "item_price", "paid_price", "price"), what="item_price"
        )
        explicit_total = _lookup(item, "line_total", "total_price")
        line_total = (
            _money(explicit_total, what="line_total")
            if explicit_total is not None
            else (unit_price * Decimal(quantity)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=_first_str(item, "name", "title", "product_name") or sku,
            quantity=quantity,
            line_total=line_total,
        )

    @staticmethod
    def _order_status(order: Mapping[str, Any]) -> str:
        """Lazada exposes either a scalar ``status`` or a ``statuses`` list."""
        scalar = _first_str(order, "status")
        if scalar:
            return scalar.lower()
        raw = _lookup(order, "statuses")
        if isinstance(raw, list):
            for value in raw:
                text = str(value or "").strip()
                if text:
                    return text.lower()
        return ""

    @staticmethod
    def _payment_status(status: str, order: Mapping[str, Any]) -> PaymentStatus:
        if status == "pending":
            return PaymentStatus.UNPAID
        if status == "returned":
            return PaymentStatus.REFUNDED
        paid_evidence = _lookup(order, "paid_at", "payment_method", "pay_time")
        if status in _CANCELLED_STATUSES:
            return PaymentStatus.REFUNDED if paid_evidence is not None else PaymentStatus.UNPAID
        return PaymentStatus.PAID

    async def _order_items(
        self, external_order_id: str, order: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        """Read the order's items, inline when present, otherwise via ``/order/items/get``."""
        inline = _lookup(order, *_ORDER_ITEM_KEYS)
        if isinstance(inline, list):
            return [item for item in inline if isinstance(item, Mapping)]
        payload = await self._request(
            "GET", LAZADA_ORDER_ITEMS_PATH, {"order_id": external_order_id}
        )
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        return self._rows(self._data_of(payload), *_ORDER_ITEM_KEYS)

    async def _normalize_order(
        self, order: Mapping[str, Any], *, fallback_updated_at: datetime
    ) -> NormalizedOrder | None:
        """Map one Lazada sales order onto the shared normalized order DTO."""
        external_order_id = _first_str(order, "order_id", "id")
        if external_order_id is None:
            logger.warning("lazada_order_missing_id", seller_id=self.seller_id)
            return None
        status = self._order_status(order)
        line_items = [
            item
            for row in await self._order_items(external_order_id, order)
            if (item := self._normalize_line_item(row)) is not None
        ]
        if not line_items:
            # ``NormalizedOrder`` requires at least one mapped line: an order whose items
            # carry no seller SKU cannot be imported honestly, so it is skipped and logged.
            logger.warning(
                "lazada_order_without_mappable_items",
                seller_id=self.seller_id,
                order_id=external_order_id,
            )
            return None

        subtotal_raw = _lookup(order, "price", "subtotal", "items_total")
        subtotal = (
            _money(subtotal_raw, what="price")
            if subtotal_raw is not None
            else _sum_money(line.line_total for line in line_items)
        )
        shipping = _money(_lookup(order, "shipping_fee", "shipping_fee_total"), what="shipping_fee")
        discount = _money(
            _lookup(order, "voucher", "voucher_seller", "seller_discount"),
            what="voucher",
        )
        tax = _money(_lookup(order, "tax_amount", "tax"), what="tax_amount")
        total_raw = _lookup(order, "total_amount", "grand_total")
        total = (
            _money(total_raw, what="total_amount")
            if total_raw is not None
            else (subtotal + shipping + tax - discount).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )
        commission = _money(
            _lookup(order, "platform_commission", "commission", "referral_fee"),
            what="platform_commission",
        )
        refunded = _money(_lookup(order, "refund_amount", "refunded_amount"), what="refund_amount")

        shipping_address = order.get("address_shipping")
        address: Mapping[str, Any] = (
            shipping_address if isinstance(shipping_address, Mapping) else {}
        )
        buyer_id = _first_str(order, "customer_id", "buyer_id")
        email = _first_str(order, "customer_email", "buyer_email") or _first_str(address, "email")
        phone = _first_str(order, "customer_phone", "buyer_phone") or _first_str(
            address, "phone", "phone2"
        )
        first_name = _first_str(order, "customer_first_name") or _first_str(address, "first_name")
        last_name = _first_str(order, "customer_last_name") or _first_str(address, "last_name")
        display_name = (
            f"{first_name or ''} {last_name or ''}".strip()
            or _first_str(order, "customer_name", "buyer_name")
            or email
            or "Lazada Buyer"
        )
        country = _first_str(order, "customer_country", "country") or _first_str(
            address, "country", "country_code"
        )

        metadata: dict[str, Any] = {
            "lazada_seller_id": self.seller_id,
            "lazada_region": self.region,
            "lazada_order_status": status,
        }
        order_number = _first_str(order, "order_number", "number")
        if order_number:
            metadata["lazada_order_number"] = order_number
        payment_method = _first_str(order, "payment_method")
        if payment_method:
            metadata["lazada_payment_method"] = payment_method
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )

        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=order_number,
            order_mode=OrderMode.B2C_RETAIL,
            currency=(_first_str(order, "currency", "currency_code") or "USD").upper(),
            # Locked to the tenant base rate at ingestion; None means "resolve from live
            # quotes", never guess.
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP),
            fulfillment_status=ORDER_STATUS_MAP.get(status, FulfillmentStatus.CONFIRMED_RESERVED),
            payment_status=self._payment_status(status, order),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone,
                external_customer_id=buyer_id,
                # The ERP has no Lazada-specific identity token yet; the opaque buyer id is
                # the same external-id bucket other channel-only ids use.
                external_identity_type=IdentityType.WEB_VISITOR_ID,
                display_name=display_name,
                country_code=country,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_lazada_datetime(
                _lookup(order, "updated_at", "update_time", "modified_time"),
                fallback_updated_at,
            ),
        )

    # ------------------------------------------------------------------ adapter API

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        offset = _offset(cursor)
        window_start = _as_utc(since)
        payload = await self._request(
            "GET",
            LAZADA_ORDERS_PATH,
            {
                "update_after": _lazada_timestamp(window_start),
                "limit": str(LAZADA_PAGE_SIZE),
                "offset": str(offset),
                "sort_by": "updated_at",
                "sort_direction": "ASC",
            },
        )
        rows = self._rows(self._data_of(payload), *_ORDER_ROW_KEYS)
        orders: list[NormalizedOrder] = []
        for row in rows:
            normalized = await self._normalize_order(row, fallback_updated_at=window_start)
            if normalized is not None:
                orders.append(normalized)
        next_cursor = str(offset + LAZADA_PAGE_SIZE) if len(rows) >= LAZADA_PAGE_SIZE else None
        return orders, next_cursor

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        offset = _offset(cursor)
        window_start = _as_utc(since)
        payload = await self._request(
            "GET",
            LAZADA_PRODUCTS_PATH,
            {
                "update_after": _lazada_timestamp(window_start),
                "limit": str(LAZADA_PAGE_SIZE),
                "offset": str(offset),
            },
        )
        rows = self._rows(self._data_of(payload), *_PRODUCT_ROW_KEYS)
        products: list[NormalizedProduct] = []
        for row in rows:
            product_id = _first_str(row, "item_id", "id", "product_id")
            if product_id is None:
                continue
            attributes = row.get("attributes")
            attrs: Mapping[str, Any] = attributes if isinstance(attributes, Mapping) else {}
            products.append(
                NormalizedProduct(
                    external_product_id=product_id,
                    title=_first_str(row, "name", "title")
                    or _first_str(attrs, "name", "title")
                    or product_id,
                    variants=self._product_variants(row),
                )
            )
        next_cursor = str(offset + LAZADA_PAGE_SIZE) if len(rows) >= LAZADA_PAGE_SIZE else None
        return products, next_cursor

    @staticmethod
    def _product_variants(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_skus = _lookup(row, "skus", "sku_list", "Skus")
        if not isinstance(raw_skus, list):
            return []
        variants: list[dict[str, Any]] = []
        for sku in raw_skus:
            if not isinstance(sku, Mapping):
                continue
            price_raw = _lookup(sku, "price", "special_price", "sale_price")
            quantity_raw = _lookup(sku, "quantity", "stock", "available")
            variants.append(
                {
                    "external_sku": _first_str(sku, "seller_sku", "SellerSku", "shop_sku") or "",
                    "external_variant_id": _first_str(sku, "sku_id", "SkuId", "id"),
                    "unit_price_usd": (
                        str(_money(price_raw, what="sku_price")) if price_raw is not None else None
                    ),
                    "stock_quantity": (
                        max(0, _to_int(quantity_raw, what="sku_quantity"))
                        if quantity_raw is not None
                        else None
                    ),
                }
            )
        return variants

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Mark one Lazada order ready to ship with its carrier and tracking number."""
        order_id = external_order_id.strip()
        if not order_id:
            raise AppError(
                slug="lazada-fulfillment-rejected",
                status_code=422,
                code="ERR_LAZADA_FULFILLMENT_REJECTED",
                title="Lazada Fulfillment Rejected",
                detail="A Lazada fulfillment needs a non-empty order id.",
            )
        params: dict[str, Any] = {
            "order_id": order_id,
            "delivery_type": LAZADA_DELIVERY_TYPE_MAP.get(self.fulfillment_mode, "dropship"),
            "shipping_provider": carrier_company.strip(),
            "tracking_number": tracking_number.strip(),
        }
        if fulfillment_order_id:
            params["package_id"] = fulfillment_order_id.strip()
        if tracking_url:
            params["tracking_url"] = tracking_url.strip()
        logger.info(
            "lazada_order_fulfillment",
            seller_id=self.seller_id,
            region=self.region,
            order_id=order_id,
            package_id=fulfillment_order_id,
            notify_customer=notify_customer,
        )
        payload = await self._request("POST", LAZADA_ORDERS_FULFILL_PATH, params)
        return self._data_of(payload)

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_lazada_webhook(raw_body, headers, secret or self._app_secret)

    # ------------------------------------------------- live product / inventory APIs

    @staticmethod
    def _primary_category(category: str) -> str:
        """Lazada needs a numeric ``PrimaryCategory``; a text category falls back safely."""
        text = str(category or "").strip()
        return text if text.isdigit() else DEFAULT_LAZADA_PRIMARY_CATEGORY

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create a single- or multi-variant Lazada product and return its receipt."""
        clean_sku = payload.external_sku.strip()
        clean_title = payload.title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="lazada-listing-rejected",
                status_code=422,
                code="ERR_LAZADA_LISTING_REJECTED",
                title="Lazada Listing Rejected",
                detail="A Lazada listing needs a non-empty SKU and title.",
            )
        # Lazada exposes no B2B MOQ ladder, so ``payload.moq_tiers`` is not forwarded; the
        # tiers stay in the ERP quote and are never invented as marketplace price breaks.
        variants = list(payload.variants)
        if payload.options and not variants:
            # Lazada expresses variations through per-SKU ``VariantAttributes``; declared
            # options with no SKU to attach them to would be silently dropped.
            raise AppError(
                slug="lazada-listing-rejected",
                status_code=422,
                code="ERR_LAZADA_LISTING_REJECTED",
                title="Lazada Listing Rejected",
                detail=(
                    "Lazada option specifications require at least one variant SKU; "
                    f"'{clean_sku}' declared options without variants."
                ),
            )
        if payload.image_blobs:
            # Raw bytes cannot reach Lazada: its product API only accepts hosted https URLs
            # and this adapter has no signed media-upload stream. Publishing anyway would
            # ship an image-less listing, so the caller is asked for hosted URLs instead.
            raise AppError(
                slug="lazada-listing-rejected",
                status_code=422,
                code="ERR_LAZADA_LISTING_REJECTED",
                title="Lazada Listing Rejected",
                detail=(
                    "Lazada listings need hosted https images; upload the "
                    f"{len(payload.image_blobs)} image blob(s) and pass their URLs in "
                    f"'images' for SKU '{clean_sku}'."
                ),
            )
        declared_options = [option.name.strip() for option in payload.options]
        for variant in variants:
            missing_options = [
                name for name in declared_options if not variant.option_values.get(name)
            ]
            if missing_options:
                # Lazada maps variations through per-SKU attributes; a variant that cannot
                # answer a declared option would silently publish the wrong variation.
                raise AppError(
                    slug="lazada-listing-rejected",
                    status_code=422,
                    code="ERR_LAZADA_LISTING_REJECTED",
                    title="Lazada Listing Rejected",
                    detail=(
                        f"Variant '{variant.external_sku}' is missing the declared "
                        f"option value(s) {', '.join(missing_options)}."
                    ),
                )

        images = _https_images(payload.images)
        attributes: dict[str, Any] = {
            "name": clean_title[:255],
            "description": _listing_description(payload.description, payload.bullet_points)[:25000],
            "brand": "OEM",
        }
        if payload.hs_code:
            attributes["HSCode"] = payload.hs_code.strip()

        skus: list[dict[str, Any]] = []
        if variants:
            for variant in variants:
                variant_sku = variant.external_sku.strip() or clean_sku
                variant_images = _https_images([variant.image_url]) or images
                sku_body: dict[str, Any] = {
                    "SellerSku": variant_sku,
                    "Price": str(_money(variant.price_usd, what="price_usd")),
                    "Quantity": str(max(0, int(variant.quantity))),
                }
                if variant_images:
                    sku_body["Images"] = variant_images
                variant_attributes = [
                    {"name": str(name), "value": str(value)}
                    for name, value in variant.option_values.items()
                ]
                if variant_attributes:
                    sku_body["VariantAttributes"] = variant_attributes
                skus.append(sku_body)
        else:
            sku_body = {
                "SellerSku": clean_sku,
                "Price": str(_money(payload.price_usd, what="price_usd")),
                "Quantity": str(max(0, int(payload.quantity))),
            }
            if images:
                sku_body["Images"] = images
            skus.append(sku_body)

        product: dict[str, Any] = {
            "PrimaryCategory": self._primary_category(payload.category),
            "Attributes": attributes,
            "Skus": skus,
        }
        if images:
            product["Images"] = images

        response = await self._request(
            "POST", LAZADA_PRODUCT_CREATE_PATH, body={"Request": {"Product": product}}
        )
        data = self._data_of(response)
        item_id = _first_str(data, "item_id", "itemId", "ItemId") or _first_str(response, "item_id")
        if item_id is None:
            raise AppError(
                slug="lazada-listing-rejected",
                status_code=502,
                code="ERR_LAZADA_LISTING_REJECTED",
                title="Lazada Listing Rejected",
                detail=self._redact(
                    "Lazada accepted the create request but returned no item_id for SKU "
                    f"'{clean_sku}'."
                ),
            )
        raw_skus = _lookup(data, "skus", "Skus", "sku_list")
        receipts: list[MarketplaceVariantReceipt] = []
        if isinstance(raw_skus, list):
            for sku_row in raw_skus:
                if not isinstance(sku_row, Mapping):
                    continue
                sku_id = _first_str(sku_row, "sku_id", "SkuId", "id")
                receipts.append(
                    MarketplaceVariantReceipt(
                        external_sku=_first_str(sku_row, "seller_sku", "SellerSku", "shop_sku")
                        or clean_sku,
                        external_variant_id=sku_id,
                        # Lazada holds inventory on the SKU itself: there is no separate
                        # inventory-item resource, so the SKU id is the inventory handle.
                        external_inventory_item_id=sku_id,
                    )
                )
        return MarketplaceListingDispatchReceipt(
            external_product_id=item_id,
            external_sku=clean_sku,
            status="PUBLISHED",
            raw_response=dict(response),
            variant_receipts=receipts,
        )

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
        """Set one Lazada SKU's absolute sellable quantity via price/quantity update."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="lazada-inventory-sync-failed",
                status_code=422,
                code="ERR_LAZADA_INVENTORY_SYNC",
                title="Lazada Inventory Sync Failed",
                detail="Lazada inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        sku_id = (
            (external_variant_id or "").strip()
            or (external_inventory_item_id or "").strip()
            or None
        )
        sku_body: dict[str, Any] = {"SellerSku": clean_sku, "Quantity": norm_qty}
        if sku_id:
            sku_body["SkuId"] = sku_id
        response = await self._request(
            "POST", LAZADA_PRICE_QUANTITY_PATH, body={"skus": [sku_body]}
        )
        data = self._data_of(response)
        return {
            "channel": "lazada",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "product_id": (external_product_id or "").strip() or None,
            "variant_id": sku_id,
            "external_inventory_item_id": sku_id,
            "location_id": (location_id or "").strip() or None,
            "warehouse_id": (warehouse_id or "").strip() or None,
            "raw": data,
        }


__all__ = [
    "DEFAULT_LAZADA_PRIMARY_CATEGORY",
    "LAZADA_AUTHORIZATION_HEADER",
    "LAZADA_DEFAULT_REGION",
    "LAZADA_DEFAULT_TIMEOUT_SECONDS",
    "LAZADA_DELIVERY_TYPE_MAP",
    "LAZADA_ORDERS_FULFILL_PATH",
    "LAZADA_ORDERS_PATH",
    "LAZADA_ORDER_ITEMS_PATH",
    "LAZADA_PAGE_SIZE",
    "LAZADA_PRICE_QUANTITY_PATH",
    "LAZADA_PRODUCTS_PATH",
    "LAZADA_PRODUCT_CREATE_PATH",
    "LAZADA_REGION_DOMAINS",
    "LAZADA_SIGNATURE_HEADER",
    "MONEY_SCALE",
    "ORDER_STATUS_MAP",
    "LazadaAdapter",
    "sign_lazada_request",
    "verify_lazada_webhook",
]
