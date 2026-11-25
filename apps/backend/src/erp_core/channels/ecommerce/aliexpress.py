"""AliExpress Open Platform (TOP) connector: DS orders, listings, stock and webhooks.

Signing follows the AliExpress canonical form: the API method name followed by every
signed parameter as ``key + value`` in ascending key order, HMAC-SHA256 with the app
secret and upper-case hex output. Webhooks are verified separately over the *raw* body
with ``x-aliexpress-hmac-sha256`` and a constant-time comparison.

Two fulfillment modes are supported:

* ``standard`` - the seller ships, so a carrier and a tracking number are mandatory.
* ``managed``  - AliExpress (Cainiao) books the label, so a carrier/tracking number is
  optional and the platform assigns them.

Money is always :class:`decimal.Decimal` with ``ROUND_HALF_UP`` and a 2dp scale; no float
ever reaches order, commission or refund math. The app secret and the access token are
never echoed back: ``__repr__`` prints ``[REDACTED]`` and every ``AppError`` detail is
scrubbed through :meth:`AliExpressAdapter._redact`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
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

ALIEXPRESS_DEFAULT_ENDPOINT_URL = "https://openapi.aliexpress.com/sync"
ALIEXPRESS_ORDER_LIST_METHOD = "aliexpress.solution.order.list.get"
ALIEXPRESS_PRODUCT_LIST_METHOD = "aliexpress.solution.product.list.get"
ALIEXPRESS_PRODUCT_CREATE_METHOD = "aliexpress.solution.product.create"
ALIEXPRESS_IMAGE_UPLOAD_METHOD = "aliexpress.solution.image.upload"
ALIEXPRESS_STOCK_UPDATE_METHOD = "aliexpress.solution.sku.stock.update"
ALIEXPRESS_SHIPMENT_CREATE_METHOD = "aliexpress.solution.shipment.create"
ALIEXPRESS_WEBHOOK_SIGNATURE_HEADER = "x-aliexpress-hmac-sha256"

#: AliExpress DS pages are fixed at 50 rows; the cursor is a 1-based page number.
ALIEXPRESS_PAGE_SIZE = 50
#: All money lands in NUMERIC(…, 2) columns; quantize once, half-up, never from floats.
MONEY_SCALE = Decimal("0.01")
#: ``standard`` ships from the seller's warehouse; ``managed`` uses AliExpress logistics.
FULFILLMENT_MODES = frozenset({"standard", "managed"})
#: ``sign`` is the only parameter excluded from its own canonical payload.
_UNSIGNED_PARAMS = frozenset({"sign"})
_SUCCESS_CODES = frozenset({"", "0", "200", "success", "true"})
_CANCELLED_STATUSES = frozenset(
    {
        "in_cancel",
        "buyer_cancel",
        "seller_cancel_close_order",
        "cancel",
        "cancelled",
        "canceled",
        "closed",
    }
)
_REFUNDED_STATUSES = frozenset({"refunded", "returned"})
_UNPAID_FLAGS = frozenset({"not_paid", "unpaid", "wait_buyer_pay"})

ORDER_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "place_order_success": FulfillmentStatus.PENDING_PAYMENT,
    "wait_seller_send_goods": FulfillmentStatus.CONFIRMED_RESERVED,
    "seller_part_send_goods": FulfillmentStatus.PARTIALLY_SHIPPED,
    "wait_buyer_accept_goods": FulfillmentStatus.SHIPPED,
    "finish": FulfillmentStatus.DELIVERED,
    "in_cancel": FulfillmentStatus.CANCELLED,
    "buyer_cancel": FulfillmentStatus.CANCELLED,
    "seller_cancel_close_order": FulfillmentStatus.CANCELLED,
    "cancel": FulfillmentStatus.CANCELLED,
    "cancelled": FulfillmentStatus.CANCELLED,
    "canceled": FulfillmentStatus.CANCELLED,
    "closed": FulfillmentStatus.CANCELLED,
    "refunded": FulfillmentStatus.REFUNDED,
    "returned": FulfillmentStatus.REFUNDED,
}

_ORDER_ROW_KEYS = ("orders", "order", "order_list", "order_list_get")
_ORDER_LINE_KEYS = ("order_line_list", "order_line", "lines", "line_items")
_PRODUCT_ROW_KEYS = ("products", "product", "product_list")
_PRODUCT_SKU_KEYS = ("sku_list", "skus", "sku")


# ----------------------------------------------------------------------------- signing


def sign_aliexpress_request(
    method: str, params: Mapping[str, Any], app_secret: str
) -> str:
    """Canonical AliExpress TOP signature: ``HmacSHA256(secret, method + sorted(k + v))``.

    ``sign`` is skipped so the helper is idempotent on an already-signed parameter map,
    which lets a verifier recompute the signature byte for byte from the wire payload.
    """
    if not app_secret:
        raise ValueError("app_secret is required to sign AliExpress requests")
    canonical = method + "".join(
        f"{key}{value}"
        for key, value in sorted(params.items())
        if key not in _UNSIGNED_PARAMS
    )
    return (
        hmac.new(app_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256)
        .hexdigest()
        .upper()
    )


def verify_aliexpress_webhook(
    raw_body: bytes, headers: Mapping[str, str], app_secret: str
) -> bool:
    """Fail-closed, constant-time verification of ``x-aliexpress-hmac-sha256``.

    The digest covers the exact bytes AliExpress sent (not a re-serialized JSON body), so
    a tampered, padded or re-encoded payload never verifies.
    """
    if not app_secret:
        return False
    received = _header(headers, ALIEXPRESS_WEBHOOK_SIGNATURE_HEADER)
    if not received:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, received.strip().lower())
    except (TypeError, ValueError):
        return False


# ----------------------------------------------------------------------------- helpers


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (AliExpress partners send mixed-cased names)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted and value:
            return str(value)
    return None


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
    """Parse an AliExpress numeric field without ever letting a binary float into money."""
    if value is None or isinstance(value, bool):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Mapping):
        return _to_decimal(_lookup(value, "amount", "value", "price"), what=what)
    text = str(value).strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise AppError(
            slug="aliexpress-api-error",
            status_code=502,
            code="ERR_ALIEXPRESS_API_ERROR",
            title="AliExpress API Error",
            detail=f"AliExpress response carried an unparseable '{what}' value.",
        ) from exc


def _money(value: Any, *, what: str = "amount") -> Decimal:
    return _to_decimal(value, what=what).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _sum_money(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal("0.00")).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _to_int(value: Any, *, what: str) -> int:
    return int(_to_decimal(value, what=what))


def _param(value: Any) -> str:
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


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _aliexpress_datetime(value: Any, fallback: datetime) -> datetime:
    """Parse AliExpress timestamps (``YYYY-MM-DD HH:MM:SS``, ISO-8601 or epoch) as UTC."""
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
        return datetime.fromtimestamp(
            epoch / 1000 if epoch > 100_000_000_000 else epoch, UTC
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            return fallback
    return _as_utc(parsed)


def _page_number(cursor: str | None) -> int:
    if cursor is None or not str(cursor).strip():
        return 1
    text = str(cursor).strip()
    if not text.isdigit() or int(text) < 1:
        raise AppError(
            slug="aliexpress-invalid-cursor",
            status_code=422,
            code="ERR_ALIEXPRESS_INVALID_CURSOR",
            title="Invalid AliExpress Cursor",
            detail="AliExpress cursors are 1-based page numbers.",
        )
    return int(text)


def _next_cursor(data: Mapping[str, Any], page: int) -> str | None:
    """Advance only on an explicit ``has_next`` or a total that covers a further page."""
    has_next = data.get("has_next")
    if has_next is not None and str(has_next).strip().lower() in {"true", "1", "yes"}:
        return str(page + 1)
    total_raw = _lookup(data, "total_record", "total_count", "total")
    if total_raw is not None:
        try:
            total = int(str(total_raw).strip())
        except ValueError:
            return None
        if total > page * ALIEXPRESS_PAGE_SIZE:
            return str(page + 1)
    return None


def _rows(source: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    """Collect row dicts from ``key: [...]`` or the TOP ``key: {child: [...]}`` shape."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows(value, *keys)
            if nested:
                return nested
    return []


def _currency_code(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    code = _lookup(value, "currency_code", "currency", "code")
    if code is None:
        return None
    text = str(code).strip().upper()
    return text if len(text) == 3 else None


def _country_code(value: Any, default: str = "US") -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 2 and text.isalpha() else default


def _option_values(source: Mapping[str, Any]) -> dict[str, str]:
    """Read declared option values from a dict or AliExpress' ``Color:Red;Size:L`` text."""
    raw = _lookup(source, "option_values", "sku_attr", "attributes")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items() if str(value).strip()}
    if isinstance(raw, str) and raw.strip():
        pairs: dict[str, str] = {}
        for chunk in raw.replace(",", ";").split(";"):
            name, _, value = chunk.partition(":")
            if name.strip() and value.strip():
                pairs[name.strip()] = value.strip()
        return pairs
    return {}


def _listing_description(description: str, bullet_points: Iterable[str]) -> str:
    body = str(description).strip()
    bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
    if not bullets:
        return body
    bullet_block = "\n".join(f"- {bullet}" for bullet in bullets)
    return "\n\n".join(part for part in (body, bullet_block) if part)


def _extract_product_id(payload: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = [payload]
    result = payload.get("result")
    if isinstance(result, Mapping):
        candidates.append(result)
        candidates.append(result.get("data"))
        candidates.append(result.get("product"))
    candidates.append(payload.get("data"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        value = _lookup(candidate, "product_id", "productId", "id")
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


class AliExpressAdapter(CommercePlatformAdapter):
    """AliExpress Open Platform adapter for DS orders, listings and stock."""

    def __init__(
        self,
        *,
        seller_id: str,
        app_key: str,
        app_secret: str,
        access_token: str,
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        required = {
            "seller_id": seller_id,
            "app_key": app_key,
            "app_secret": app_secret,
            "access_token": access_token,
        }
        for field_name, value in required.items():
            if not str(value or "").strip():
                raise ValueError(f"{field_name} is required for the AliExpress adapter")
        mode = str(fulfillment_mode or "").strip().lower()
        if mode not in FULFILLMENT_MODES:
            raise ValueError("fulfillment_mode must be 'standard' or 'managed'")
        self.seller_id = seller_id.strip()
        self.app_key = app_key.strip()
        self.fulfillment_mode = mode
        self.endpoint_url = (endpoint_url or ALIEXPRESS_DEFAULT_ENDPOINT_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._app_secret = app_secret
        self._access_token = access_token
        #: Test seam: a ``MockTransport`` here means zero real network traffic.
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"AliExpressAdapter(seller_id={self.seller_id!r}, "
            f"fulfillment_mode={self.fulfillment_mode!r}, app_key={self.app_key!r}, "
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

    @staticmethod
    def _error_signals(payload: Mapping[str, Any]) -> tuple[str, str]:
        sources: list[Any] = []
        error_response = payload.get("error_response")
        if isinstance(error_response, Mapping):
            sources.append(error_response)
        sources.append(payload)
        result = payload.get("result")
        if isinstance(result, Mapping):
            sources.append(result)
        code = ""
        message = ""
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            if not code:
                code = _first_str(source, "code", "error_code", "errorCode", "sub_code") or ""
            if not message:
                message = (
                    _first_str(source, "msg", "message", "error_message", "sub_msg") or ""
                )
            if code or message:
                break
        return code, message

    def _raise_for_failure(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        raw_text: str,
        *,
        method: str,
    ) -> None:
        code, message = self._error_signals(payload)
        result = payload.get("result")
        failed = (
            status_code >= 400
            or not payload
            or isinstance(payload.get("error_response"), Mapping)
            or code.strip().lower() not in _SUCCESS_CODES
            or payload.get("success") is False
            or (isinstance(result, Mapping) and result.get("success") is False)
        )
        if not failed:
            return

        haystack = f"{code} {message}".lower()
        auth_failed = status_code in {401, 403} or any(
            token in haystack
            for token in (
                "invalidsignature",
                "invalid signature",
                "invalid_session",
                "illegalaccesstoken",
                "signature",
                "access token",
                "unauthorized",
                "forbidden",
                "permission",
            )
        )
        rate_limited = status_code == 429 or any(
            token in haystack
            for token in ("throttl", "rate limit", "ratelimit", "flow control", "flowcontrol")
        )
        reason = message or raw_text.strip()[:200] or "empty response body"
        detail = self._redact(
            f"AliExpress {method} failed (http_status={status_code}, "
            f"code={code or 'unknown'}): {reason}"
        )[:500]
        logger.warning(
            "aliexpress_request_failed",
            seller_id=self.seller_id,
            method=method,
            http_status=status_code,
            aliexpress_code=code,
            auth_failed=auth_failed,
            rate_limited=rate_limited,
        )
        if auth_failed:
            raise AppError(
                slug="aliexpress-auth-failed",
                status_code=502,
                code="ERR_ALIEXPRESS_AUTH_FAILED",
                title="AliExpress Authentication Failed",
                detail=detail,
            )
        if rate_limited:
            raise AppError(
                slug="aliexpress-rate-limited",
                status_code=429,
                code="ERR_ALIEXPRESS_RATE_LIMITED",
                title="AliExpress Rate Limited",
                detail=detail,
            )
        raise AppError(
            slug="aliexpress-api-error",
            status_code=502,
            code="ERR_ALIEXPRESS_API_ERROR",
            title="AliExpress API Error",
            detail=detail,
        )

    async def _call_api(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sign and execute one TOP call, mapping the envelope to typed ``AppError``s."""
        signed: dict[str, str] = {
            "method": method,
            "app_key": self.app_key,
            "session": self._access_token,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "v": "2.0",
            "sign_method": "hmac-sha256",
        }
        for key, value in (params or {}).items():
            if key in _UNSIGNED_PARAMS or value is None:
                continue
            signed[key] = _param(value)
        signed["sign"] = sign_aliexpress_request(method, signed, self._app_secret)

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.post(
                    self.endpoint_url, data=signed, headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "aliexpress_transport_error",
                seller_id=self.seller_id,
                method=method,
                error=self._redact(str(exc))[:300],
            )
            raise AppError(
                slug="aliexpress-api-error",
                status_code=502,
                code="ERR_ALIEXPRESS_API_ERROR",
                title="AliExpress API Error",
                detail=self._redact(
                    f"AliExpress {method} failed before a response was received; the "
                    "request can be retried."
                ),
            ) from exc

        raw_text = self._redact(response.text)
        payload = self._parse_payload(response)
        self._raise_for_failure(response.status_code, payload, raw_text, method=method)
        return payload

    @staticmethod
    def _result_of(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Unwrap the TOP envelope: ``<method>_response`` -> ``result`` -> the payload."""
        envelope: Mapping[str, Any] = payload
        for key, value in payload.items():
            if key.endswith("_response") and isinstance(value, Mapping):
                envelope = value
                break
        result = envelope.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        return dict(envelope)

    # ---------------------------------------------------------------- normalization

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        sku = _first_str(item, "sku_code", "sku_id", "sku", "product_sku")
        if sku is None:
            logger.warning(
                "aliexpress_line_item_missing_sku",
                seller_id=self.seller_id,
                product_id=_first_str(item, "product_id"),
            )
            return None
        quantity = max(
            1, _to_int(_lookup(item, "product_count", "quantity", "num"), what="quantity")
        )
        # Keep full per-unit precision: quantizing before multiplying drifts multi-unit lines.
        unit_price = _to_decimal(
            _lookup(item, "product_price", "unit_price", "price"), what="unit_price"
        )
        explicit_total = _lookup(
            item, "order_line_total", "line_total", "product_amount", "total_price"
        )
        line_total = (
            _money(explicit_total, what="line_total")
            if explicit_total is not None
            else (unit_price * Decimal(quantity)).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=_first_str(item, "product_name", "subject", "title") or sku,
            quantity=quantity,
            line_total=line_total,
        )

    @staticmethod
    def _payment_status(
        status: str,
        payment_flag: str,
        order: Mapping[str, Any],
        *,
        refunded: Decimal = Decimal("0.00"),
        total: Decimal = Decimal("0.00"),
    ) -> PaymentStatus:
        if payment_flag in _UNPAID_FLAGS:
            return PaymentStatus.UNPAID
        if status in _REFUNDED_STATUSES:
            return PaymentStatus.REFUNDED
        if status in _CANCELLED_STATUSES:
            paid_evidence = _lookup(
                order, "pay_time", "gmt_pay_success", "paid_time", "gmt_pay"
            )
            # A PAID flag is itself paid evidence: a cancelled paid order is a refund.
            was_paid = (
                payment_flag == "paid"
                or paid_evidence is not None
                or refunded > Decimal("0.00")
            )
            return PaymentStatus.REFUNDED if was_paid else PaymentStatus.UNPAID
        if total > Decimal("0.00") and refunded >= total:
            return PaymentStatus.REFUNDED
        return PaymentStatus.PAID

    def _normalize_order(
        self, order: Mapping[str, Any], *, fallback_updated_at: datetime
    ) -> NormalizedOrder | None:
        """Map one AliExpress DS order onto the shared B2C order DTO."""
        external_order_id = _first_str(order, "order_id", "trade_id", "id")
        if external_order_id is None:
            logger.warning("aliexpress_order_missing_id", seller_id=self.seller_id)
            return None
        status = (_first_str(order, "order_status", "status") or "").lower()
        payment_flag = (_first_str(order, "payment_status") or "").lower()

        line_items = [
            item
            for row in _rows(order, *_ORDER_LINE_KEYS)
            if (item := self._normalize_line_item(row)) is not None
        ]
        if not line_items:
            logger.warning(
                "aliexpress_order_without_mappable_items",
                seller_id=self.seller_id,
                order_id=external_order_id,
            )
            return None

        currency = (
            _currency_code(_lookup(order, "order_amount", "payment_amount"))
            or _first_str(order, "currency", "currency_code")
            or "USD"
        ).upper()
        subtotal_raw = _lookup(order, "product_amount", "subtotal", "goods_amount")
        subtotal = (
            _money(subtotal_raw, what="product_amount")
            if subtotal_raw is not None
            else _sum_money(line.line_total for line in line_items)
        )
        shipping = _money(
            _lookup(order, "shipping_fee", "freight_amount", "logistics_fee"),
            what="shipping_fee",
        )
        discount = _money(
            _lookup(order, "seller_discount", "discount_amount", "platform_discount"),
            what="discount_amount",
        )
        tax = _money(
            _lookup(order, "tax_amount", "tax_collected", "vat"), what="tax_amount"
        )
        total_raw = _lookup(order, "order_amount", "total_amount", "payment_amount")
        total = (
            _money(total_raw, what="order_amount")
            if total_raw is not None
            else (subtotal + shipping + tax - discount).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )
        commission = _money(
            _lookup(order, "platform_commission", "commission_amount"),
            what="platform_commission",
        )
        refunded = _money(
            _lookup(order, "refund_amount", "refunded_amount"), what="refund_amount"
        )

        receiver = order.get("receipt_address")
        address: Mapping[str, Any] = receiver if isinstance(receiver, Mapping) else {}
        buyer_login = _first_str(order, "buyer_login_id", "buyer_id", "buyer_open_id")
        email = _first_str(order, "buyer_email", "email")
        country = _country_code(
            _first_str(order, "buyer_country", "country_code", "country")
            or _first_str(address, "country", "country_code")
        )

        metadata: dict[str, Any] = {
            "aliexpress_order_status": status,
            "aliexpress_seller_id": self.seller_id,
            "aliexpress_fulfillment_mode": self.fulfillment_mode,
        }
        order_number = _first_str(order, "order_no", "trade_no")
        if order_number:
            metadata["aliexpress_order_no"] = order_number
        if buyer_login:
            metadata["aliexpress_buyer_login_id"] = buyer_login
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )

        full_refund = total > Decimal("0.00") and refunded >= total
        if status in _CANCELLED_STATUSES:
            fulfillment_status = FulfillmentStatus.CANCELLED
        elif status in _REFUNDED_STATUSES or full_refund:
            fulfillment_status = FulfillmentStatus.REFUNDED
        else:
            fulfillment_status = ORDER_STATUS_MAP.get(
                status, FulfillmentStatus.CONFIRMED_RESERVED
            )

        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=order_number,
            order_mode=OrderMode.B2C_RETAIL,
            currency=currency,
            # Locked to the tenant base rate at ingestion: None means "resolve it
            # honestly from live quotes", never guess.
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission.quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=self._payment_status(
                status, payment_flag, order, refunded=refunded, total=total
            ),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=_first_str(order, "buyer_phone", "phone")
                or _first_str(address, "phone_number", "phone"),
                external_customer_id=buyer_login,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=buyer_login or email or "AliExpress Buyer",
                country_code=country,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_aliexpress_datetime(
                _lookup(order, "gmt_modified", "modified_time", "update_time", "gmt_update"),
                fallback_updated_at,
            ),
        )

    @staticmethod
    def _product_variants(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        for sku in _rows(row, *_PRODUCT_SKU_KEYS):
            price_raw = _lookup(sku, "price", "sku_price", "unit_price")
            stock_raw = _lookup(sku, "stock", "sku_stock", "quantity", "inventory")
            variants.append(
                {
                    "external_sku": _first_str(sku, "sku_code", "sku", "code", "sku_id") or "",
                    "external_variant_id": _first_str(sku, "sku_id", "id"),
                    "unit_price_usd": (
                        str(_money(price_raw, what="sku_price"))
                        if price_raw is not None
                        else None
                    ),
                    "stock_quantity": (
                        max(0, _to_int(stock_raw, what="sku_stock"))
                        if stock_raw is not None
                        else None
                    ),
                    "option_values": _option_values(sku),
                }
            )
        return variants

    # ------------------------------------------------------------------ adapter API

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._call_api(
            ALIEXPRESS_ORDER_LIST_METHOD,
            {
                "modify_date_start": window_start.strftime("%Y-%m-%d %H:%M:%S"),
                "page": str(page),
                "page_size": str(ALIEXPRESS_PAGE_SIZE),
            },
        )
        data = self._result_of(payload)
        orders = [
            normalized
            for row in _rows(data, *_ORDER_ROW_KEYS)
            if (normalized := self._normalize_order(row, fallback_updated_at=window_start))
            is not None
        ]
        return orders, _next_cursor(data, page)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._call_api(
            ALIEXPRESS_PRODUCT_LIST_METHOD,
            {
                "modify_date_start": window_start.strftime("%Y-%m-%d %H:%M:%S"),
                "page": str(page),
                "page_size": str(ALIEXPRESS_PAGE_SIZE),
            },
        )
        data = self._result_of(payload)
        products: list[NormalizedProduct] = []
        for row in _rows(data, *_PRODUCT_ROW_KEYS):
            product_id = _first_str(row, "product_id", "productId", "id")
            if product_id is None:
                continue
            products.append(
                NormalizedProduct(
                    external_product_id=product_id,
                    title=_first_str(row, "product_title", "subject", "title")
                    or product_id,
                    variants=self._product_variants(row),
                )
            )
        return products, _next_cursor(data, page)

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Declare shipment for one order as seller-shipped or managed logistics."""
        order_id = str(external_order_id or "").strip()
        if not order_id:
            raise AppError(
                slug="aliexpress-fulfillment-invalid-order",
                status_code=422,
                code="ERR_ALIEXPRESS_FULFILLMENT_INVALID_ORDER",
                title="Invalid AliExpress Fulfillment Order",
                detail="AliExpress fulfillment requires a non-empty order id.",
            )
        clean_tracking = str(tracking_number or "").strip()
        clean_carrier = str(carrier_company or "").strip()
        params: dict[str, Any] = {
            "order_id": order_id,
            "shipment_mode": self.fulfillment_mode,
            "notify_customer": notify_customer,
        }
        if self.fulfillment_mode == "standard":
            if not clean_tracking or not clean_carrier:
                raise AppError(
                    slug="aliexpress-tracking-required",
                    status_code=422,
                    code="ERR_ALIEXPRESS_TRACKING_REQUIRED",
                    title="AliExpress Tracking Required",
                    detail=(
                        "Standard fulfillment requires both a tracking number and a "
                        "carrier; switch to fulfillment_mode='managed' to let AliExpress "
                        "book the label."
                    ),
                )
            params["tracking_number"] = clean_tracking
            params["logistics_service_name"] = clean_carrier
        else:
            # Managed logistics assigns the carrier and AWB; only send what we were given.
            if clean_tracking:
                params["tracking_number"] = clean_tracking
            if clean_carrier:
                params["logistics_service_name"] = clean_carrier
        if tracking_url:
            params["tracking_url"] = str(tracking_url).strip()
        if fulfillment_order_id:
            params["fulfillment_order_id"] = str(fulfillment_order_id).strip()

        logger.info(
            "aliexpress_shipment_create",
            seller_id=self.seller_id,
            order_id=order_id,
            shipment_mode=self.fulfillment_mode,
        )
        payload = await self._call_api(ALIEXPRESS_SHIPMENT_CREATE_METHOD, params)
        data = self._result_of(payload)
        data.setdefault("order_id", order_id)
        data.setdefault("shipment_mode", self.fulfillment_mode)
        fulfillment_id = (
            _first_str(data, "fulfillment_id", "shipment_id", "awb_number")
            or str(fulfillment_order_id or "").strip()
            or order_id
        )
        data.setdefault("fulfillment_id", fulfillment_id)
        return data

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        return verify_aliexpress_webhook(raw_body, headers, secret or self._app_secret)

    # ------------------------------------------------- live product / inventory APIs

    async def _upload_image_blob(self, blob: MarketplaceImageBlob) -> str | None:
        """Upload one image blob and return the AliExpress-hosted URL."""
        if not blob.data:
            return None
        payload = await self._call_api(
            ALIEXPRESS_IMAGE_UPLOAD_METHOD,
            {
                "image_name": blob.filename,
                "image_mime_type": blob.mime_type,
                "image_base64": base64.b64encode(blob.data).decode("ascii"),
            },
        )
        data = self._result_of(payload)
        url = _first_str(data, "image_url", "url", "uri")
        if url is None:
            raise AppError(
                slug="aliexpress-media-upload-failed",
                status_code=502,
                code="ERR_ALIEXPRESS_MEDIA_UPLOAD_FAILED",
                title="AliExpress Image Upload Failed",
                detail=self._redact(
                    "AliExpress accepted the image upload but returned no image URL for "
                    f"'{blob.filename}'."
                ),
            )
        return url

    @staticmethod
    def _listing_skus(
        payload: MarketplaceListingPayload, *, fallback_sku: str, fallback_price: Decimal
    ) -> list[dict[str, Any]]:
        """Build the multi-variant SKU array, honouring declared option names."""
        declared = {option.name for option in payload.options}
        rows: list[dict[str, Any]] = []
        for variant in payload.variants:
            option_values = {
                str(key): str(value) for key, value in variant.option_values.items()
            }
            unknown = sorted(set(option_values) - declared)
            if declared and unknown:
                raise AppError(
                    slug="aliexpress-listing-invalid-option",
                    status_code=422,
                    code="ERR_ALIEXPRESS_LISTING_INVALID_OPTION",
                    title="Invalid AliExpress Listing Option",
                    detail=(
                        f"Variant '{variant.external_sku}' uses undeclared option(s) "
                        f"{unknown}; declared options are {sorted(declared)}."
                    ),
                )
            row: dict[str, Any] = {
                "sku_code": (variant.external_sku or "").strip() or fallback_sku,
                "price": str(_money(variant.price_usd, what="variant.price_usd")),
                "currency_code": "USD",
                "stock": max(0, int(variant.quantity)),
            }
            if option_values:
                row["option_values"] = option_values
            if variant.image_url:
                row["image_url"] = variant.image_url.strip()
            rows.append(row)
        if rows:
            return rows
        return [
            {
                "sku_code": fallback_sku,
                "price": str(fallback_price),
                "currency_code": "USD",
                "stock": max(0, int(payload.quantity)),
            }
        ]

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create one single- or multi-variant AliExpress listing and prove it landed."""
        clean_sku = payload.external_sku.strip()
        clean_title = payload.title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="aliexpress-listing-rejected",
                status_code=422,
                code="ERR_ALIEXPRESS_LISTING_REJECTED",
                title="AliExpress Listing Rejected",
                detail=(
                    "An AliExpress listing needs a non-empty external SKU and title."
                ),
            )
        price = _money(payload.price_usd, what="price_usd")
        image_urls = [
            url.strip()
            for url in payload.images
            if str(url or "").strip().startswith("https://")
        ][:9]
        for blob in payload.image_blobs:
            uploaded = await self._upload_image_blob(blob)
            if uploaded and uploaded not in image_urls and len(image_urls) < 9:
                image_urls.append(uploaded)

        body: dict[str, Any] = {
            "product_title": clean_title[:128],
            "description": _listing_description(
                payload.description, payload.bullet_points
            )[:5000],
            "category_id": payload.category.strip(),
            "image_urls": image_urls,
            "options": [
                {"name": option.name, "values": list(option.values)}
                for option in payload.options
            ],
            "skus": self._listing_skus(
                payload, fallback_sku=clean_sku, fallback_price=price
            ),
        }
        if payload.hs_code:
            body["hs_code"] = payload.hs_code.strip()
        if payload.moq_tiers:
            body["wholesale_ladder"] = [
                {
                    "min_qty": tier.min_qty,
                    "unit_price_usd": str(_money(tier.unit_price_usd, what="moq_tier")),
                }
                for tier in payload.moq_tiers
            ]

        response = await self._call_api(
            ALIEXPRESS_PRODUCT_CREATE_METHOD,
            {"product_body": json.dumps(body, separators=(",", ":"), sort_keys=True)},
        )
        data = self._result_of(response)
        product_id = _extract_product_id(data)
        if product_id is None:
            raise AppError(
                slug="aliexpress-listing-rejected",
                status_code=502,
                code="ERR_ALIEXPRESS_LISTING_REJECTED",
                title="AliExpress Listing Rejected",
                detail=self._redact(
                    "AliExpress accepted the product request but returned no product_id "
                    f"for SKU '{clean_sku}'."
                ),
            )
        variant_receipts = [
            MarketplaceVariantReceipt(
                external_sku=_first_str(sku, "sku_code", "external_sku", "sku")
                or clean_sku,
                external_variant_id=_first_str(sku, "sku_id", "id"),
                external_inventory_item_id=_first_str(sku, "inventory_item_id"),
            )
            for sku in _rows(data, *_PRODUCT_SKU_KEYS)
        ]
        if not variant_receipts:
            variant_receipts = [
                MarketplaceVariantReceipt(external_sku=clean_sku),
            ]
        return MarketplaceListingDispatchReceipt(
            external_product_id=product_id,
            external_sku=clean_sku,
            status="PUBLISHED",
            raw_response=data,
            variant_receipts=variant_receipts,
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
        """Push an absolute stock level for one AliExpress SKU."""
        clean_sku = str(external_sku or "").strip()
        if not clean_sku:
            raise AppError(
                slug="aliexpress-inventory-sync-failed",
                status_code=422,
                code="ERR_ALIEXPRESS_INVENTORY_SYNC",
                title="AliExpress Inventory Sync Failed",
                detail="AliExpress inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        params: dict[str, Any] = {"sku_code": clean_sku, "stock": str(norm_qty)}
        optional = {
            "product_id": external_product_id,
            "sku_id": external_variant_id,
            "inventory_item_id": external_inventory_item_id,
            "location_id": location_id,
            "warehouse_id": warehouse_id,
        }
        for key, value in optional.items():
            text = str(value or "").strip()
            if text:
                params[key] = text

        payload = await self._call_api(ALIEXPRESS_STOCK_UPDATE_METHOD, params)
        return {
            "channel": "aliexpress",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_product_id": external_product_id,
            "external_variant_id": external_variant_id,
            "external_inventory_item_id": external_inventory_item_id,
            "location_id": location_id,
            "warehouse_id": warehouse_id,
            "raw": self._result_of(payload),
        }
