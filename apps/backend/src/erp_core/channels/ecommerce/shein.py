"""SHEIN Open Platform connector: signed calls, order/product sync, listing + stock push.

Request signing follows SHEIN's Open Platform envelope: ``HMAC-SHA256(secret_key,
app_id + timestamp + request_path + body)`` rendered as lower-case hex in
``x-lt-signature`` next to ``x-lt-appid`` / ``x-lt-timestamp``. Webhooks use a separate
scheme and are verified over the *raw* request body through
:func:`verify_shein_webhook` (``x-shein-signature``). Money is always
:class:`decimal.Decimal` with ``ROUND_HALF_UP`` at 2dp — never IEEE-754 floats — and the
secret key never appears in ``__repr__`` or in a raised :class:`AppError`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
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

SHEIN_DEFAULT_ENDPOINT = "https://openapi.sheincorp.com"
#: Regional gateways; an unknown region degrades to the global gateway, never to a guess.
SHEIN_REGIONAL_ENDPOINTS: Mapping[str, str] = {
    "US": SHEIN_DEFAULT_ENDPOINT,
    "EU": "https://openapi.sheincorp.eu",
    "GLOBAL": SHEIN_DEFAULT_ENDPOINT,
}

SHEIN_ORDER_LIST_PATH = "/open-api/order/order-list"
SHEIN_ORDER_DETAIL_PATH = "/open-api/order/order-detail"
SHEIN_ORDER_SHIP_PATH = "/open-api/order/order-ship"
SHEIN_PRODUCT_LIST_PATH = "/open-api/goods/product-list"
SHEIN_PRODUCT_CREATE_PATH = "/open-api/goods/product"
SHEIN_PRODUCT_IMAGE_PATH = "/open-api/goods/image/upload"
SHEIN_INVENTORY_UPDATE_PATH = "/open-api/goods/inventory/update"

SHEIN_APP_ID_HEADER = "x-lt-appid"
SHEIN_TIMESTAMP_HEADER = "x-lt-timestamp"
SHEIN_REQUEST_SIGNATURE_HEADER = "x-lt-signature"
SHEIN_WEBHOOK_SIGNATURE_HEADER = "x-shein-signature"

#: SHEIN pages are fixed at 50 rows and the cursor is a 1-based page number.
SHEIN_PAGE_SIZE = 50
#: Every monetary column is NUMERIC(…, 2): quantize once, half-up, never from a float.
MONEY_SCALE = Decimal("0.01")

FULFILLMENT_MODES = frozenset({"standard", "semi_managed"})
SHEIN_SELF_SHIPPING = "self_shipping"
SHEIN_SEMI_MANAGED_SHIPPING = "semi_managed"

_SUCCESS_CODES = frozenset({"0", "200", "success", "ok", "true"})
_TRUTHY = frozenset({"true", "1", "yes", "y"})

ORDER_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "1": FulfillmentStatus.PENDING_PAYMENT,
    "2": FulfillmentStatus.CONFIRMED_RESERVED,
    "3": FulfillmentStatus.SHIPPED,
    "4": FulfillmentStatus.DELIVERED,
    "5": FulfillmentStatus.CANCELLED,
    "6": FulfillmentStatus.REFUNDED,
    "7": FulfillmentStatus.CONFIRMED_RESERVED,
    "pending": FulfillmentStatus.PENDING_PAYMENT,
    "unpaid": FulfillmentStatus.PENDING_PAYMENT,
    "pending_payment": FulfillmentStatus.PENDING_PAYMENT,
    "awaiting_shipment": FulfillmentStatus.CONFIRMED_RESERVED,
    "waiting_shipment": FulfillmentStatus.CONFIRMED_RESERVED,
    "processing": FulfillmentStatus.CONFIRMED_RESERVED,
    "shipped": FulfillmentStatus.SHIPPED,
    "in_transit": FulfillmentStatus.SHIPPED,
    "delivered": FulfillmentStatus.DELIVERED,
    "signed": FulfillmentStatus.DELIVERED,
    "cancelled": FulfillmentStatus.CANCELLED,
    "canceled": FulfillmentStatus.CANCELLED,
    "refunded": FulfillmentStatus.REFUNDED,
}

_ORDER_ROW_KEYS = (
    "orderList",
    "order_list",
    "orders",
    "orderInfoList",
    "list",
    "data",
)
_ORDER_LINE_KEYS = (
    "goodsList",
    "goods_list",
    "orderGoodsList",
    "goodsItems",
    "lineItems",
    "items",
    "skus",
)
_ORDER_DETAIL_KEYS = ("orderDetail", "order_detail", "orderInfo", "order", "data")
_PRODUCT_ROW_KEYS = (
    "productList",
    "product_list",
    "goodsList",
    "goods_list",
    "products",
    "list",
    "data",
)
_PRODUCT_VARIANT_KEYS = ("skuList", "sku_list", "skus", "variants", "skuInfoList")
_COMMISSION_KEYS = ("platformCommission", "commissionAmount", "platformFee", "commission")
_REFUND_KEYS = ("refundAmount", "refundedAmount", "refundTotal", "refunded_total")


# ------------------------------------------------------------------------- primitives


def sign_shein_request(
    *,
    app_id: str,
    secret_key: str,
    api_path: str,
    timestamp: str,
    body_str: str = "",
) -> str:
    """Canonical SHEIN signature: ``HMAC-SHA256(secret, app_id + timestamp + path + body)``."""
    if not app_id or not secret_key:
        raise ValueError("app_id and secret_key are required to sign SHEIN requests")
    message = f"{app_id}{timestamp}{api_path}{body_str}"
    return hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_shein_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    """Fail-closed, constant-time verification of ``x-shein-signature`` over the raw body."""
    received = _header(headers, SHEIN_WEBHOOK_SIGNATURE_HEADER)
    if not secret or received is None:
        return False
    candidate = received.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256=") :].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, candidate.lower())
    except (TypeError, ValueError):
        return False


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (gateways re-case ``x-`` headers unpredictably)."""
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
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _to_int(value: Any, *, what: str) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError) as exc:
        raise AppError(
            slug="shein-invalid-number",
            status_code=422,
            code="ERR_SHEIN_INVALID_NUMBER",
            title="Invalid SHEIN Number",
            detail=f"SHEIN field '{what}' is not a whole number.",
        ) from exc


def _to_decimal(value: Any, *, what: str) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise AppError(
            slug="shein-invalid-amount",
            status_code=422,
            code="ERR_SHEIN_INVALID_AMOUNT",
            title="Invalid SHEIN Amount",
            detail=f"SHEIN field '{what}' is not a decimal amount.",
        ) from exc


def _money(value: Any, *, what: str) -> Decimal:
    return _to_decimal(value, what=what).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _money_from(source: Mapping[str, Any], *keys: str) -> Decimal:
    raw = _lookup(source, *keys)
    return _money(raw, what=keys[0]) if raw is not None else Decimal("0.00")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _epoch_datetime(amount: int) -> datetime:
    seconds = amount / 1000 if amount > 10**11 else amount
    return datetime.fromtimestamp(seconds, UTC)


def _shein_datetime(value: Any, fallback: datetime) -> datetime:
    """Parse SHEIN timestamps (epoch seconds, epoch millis, ISO-8601, ``%Y-%m-%d %H:%M:%S``)."""
    if value is None or value == "" or isinstance(value, bool):
        return fallback
    if isinstance(value, int | float):
        return _epoch_datetime(int(value))
    text = str(value).strip()
    if not text:
        return fallback
    if text.isdigit():
        return _epoch_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return fallback
    return _as_utc(parsed)


def _page_number(cursor: str | None) -> int:
    if cursor is None or not str(cursor).strip():
        return 1
    text = str(cursor).strip()
    if not text.isdigit() or int(text) < 1:
        raise AppError(
            slug="shein-invalid-cursor",
            status_code=422,
            code="ERR_SHEIN_INVALID_CURSOR",
            title="Invalid SHEIN Cursor",
            detail="SHEIN Open Platform cursors are 1-based page numbers.",
        )
    return int(text)


def _is_truthy(value: Any) -> bool:
    return value is not None and str(value).strip().lower() in _TRUTHY


def _next_cursor(data: Mapping[str, Any], page: int, row_count: int) -> str | None:
    """Advance only on an explicit ``hasNext`` or a total that covers a further page."""
    has_next = _lookup(data, "hasNext", "has_next", "hasMore", "has_more")
    if has_next is not None:
        return str(page + 1) if _is_truthy(has_next) else None
    total_raw = _lookup(data, "total", "totalCount", "total_count", "count")
    if total_raw is not None:
        try:
            total = int(str(total_raw).strip())
        except ValueError:
            return None
        return str(page + 1) if total > page * SHEIN_PAGE_SIZE else None
    return str(page + 1) if row_count >= SHEIN_PAGE_SIZE else None


def _listing_description(description: str, bullet_points: Sequence[str]) -> str:
    body = str(description).strip()
    bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
    if not bullets:
        return body
    bullet_block = "\n".join(f"- {bullet}" for bullet in bullets)
    return "\n\n".join(part for part in (body, bullet_block) if part)


def _country_code(value: Any, default: str = "US") -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 2 and text.isalpha() else default


def _extract_product_id(data: Mapping[str, Any]) -> str | None:
    candidates: list[Mapping[str, Any]] = [data]
    for key in ("product", "goods", "productInfo"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        value = _lookup(candidate, "goodsId", "productId", "product_id", "goodsSn", "spuName")
        if value is not None:
            return str(value).strip() or None
    return None


class SheinAdapter(CommercePlatformAdapter):
    """SHEIN Open Platform adapter (standard seller shipping + semi-managed warehouse)."""

    def __init__(
        self,
        *,
        seller_id: str,
        open_key_id: str,
        secret_key: str,
        region: str = "US",
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        clean_seller = seller_id.strip()
        clean_app_id = open_key_id.strip()
        clean_secret = secret_key.strip()
        if not clean_seller or not clean_app_id or not clean_secret:
            raise ValueError(
                "seller_id, open_key_id and secret_key are required for the SHEIN adapter"
            )
        mode = fulfillment_mode.strip().lower()
        if mode not in FULFILLMENT_MODES:
            raise ValueError(
                f"fulfillment_mode must be one of {sorted(FULFILLMENT_MODES)}, got "
                f"'{fulfillment_mode}'"
            )
        self.seller_id = clean_seller
        self.open_key_id = clean_app_id
        self._secret_key = clean_secret
        self.region = region.strip().upper() or "US"
        self.fulfillment_mode = mode
        self.endpoint_url = (
            endpoint_url or SHEIN_REGIONAL_ENDPOINTS.get(self.region, SHEIN_DEFAULT_ENDPOINT)
        ).rstrip("/")
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"SheinAdapter(seller_id={self.seller_id!r}, open_key_id={self.open_key_id!r}, "
            f"region={self.region!r}, fulfillment_mode={self.fulfillment_mode!r}, "
            "secret_key='[REDACTED]')"
        )

    # ------------------------------------------------------------------ transport

    def _redact(self, text: str) -> str:
        """Strip the secret the adapter holds out of anything user-visible."""
        return text.replace(self._secret_key, "[REDACTED]") if self._secret_key else text

    def _client(self) -> httpx.AsyncClient:
        #: ``transport`` is the test seam: a ``MockTransport`` means zero network traffic.
        return httpx.AsyncClient(transport=self.transport, timeout=self.timeout_seconds)

    @staticmethod
    def _parse_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _data_of(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Unwrap the SHEIN envelope: ``info`` -> ``data`` -> ``result`` -> payload."""
        for key in ("info", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
        return dict(payload)

    @staticmethod
    def _rows(source: Mapping[str, Any], keys: Sequence[str]) -> list[Mapping[str, Any]]:
        for key in keys:
            raw = source.get(key)
            if isinstance(raw, list):
                return [row for row in raw if isinstance(row, Mapping)]
        return []

    @staticmethod
    def _error_signals(payload: Mapping[str, Any]) -> tuple[str, str]:
        code = _first_str(payload, "code", "errorCode", "error_code") or ""
        message = _first_str(payload, "msg", "message", "errorMsg", "error_msg") or ""
        if not code or not message:
            for key in ("info", "data", "error", "errorResponse"):
                nested = payload.get(key)
                if not isinstance(nested, Mapping):
                    continue
                code = code or _first_str(nested, "code", "errorCode", "error_code") or ""
                message = (
                    message or _first_str(nested, "msg", "message", "errorMsg", "error_msg") or ""
                )
        return code, message

    def _raise_for_failure(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        raw_text: str,
        *,
        method: str,
        api_path: str,
    ) -> None:
        code, message = self._error_signals(payload)
        failed = (
            status_code >= 400
            or payload.get("success") is False
            or (bool(code.strip()) and code.strip().lower() not in _SUCCESS_CODES)
        )
        if not failed:
            return

        # Auth/rate classification looks only at protocol signals: a business error whose
        # message happens to quote a credential must not be misreported as a login failure.
        haystack = f"{code} {message}".lower()
        auth_failed = status_code in {401, 403} or any(
            token in haystack
            for token in (
                "signature",
                "sign check",
                "appid",
                "app id",
                "unauthorized",
                "forbidden",
                "permission denied",
                "access token",
                "invalid key",
            )
        )
        rate_limited = status_code == 429 or any(
            token in haystack
            for token in ("throttl", "rate limit", "ratelimit", "too many requests", "flow limit")
        )
        reason = message or raw_text.strip()[:200] or "empty response body"
        detail = self._redact(
            f"SHEIN Open Platform {method} {api_path} failed (http_status={status_code}, "
            f"code={code or 'unknown'}): {reason}"
        )[:500]
        logger.warning(
            "shein_request_failed",
            seller_id=self.seller_id,
            api_path=api_path,
            http_status=status_code,
            shein_code=code,
            auth_failed=auth_failed,
            rate_limited=rate_limited,
        )
        if auth_failed:
            raise AppError(
                slug="shein-auth-failed",
                status_code=502,
                code="ERR_SHEIN_AUTH_FAILED",
                title="SHEIN Authentication Failed",
                detail=detail,
            )
        if rate_limited:
            raise AppError(
                slug="shein-rate-limited",
                status_code=429,
                code="ERR_SHEIN_RATE_LIMITED",
                title="SHEIN Rate Limited",
                detail=detail,
            )
        raise AppError(
            slug="shein-api-error",
            status_code=502,
            code="ERR_SHEIN_API_ERROR",
            title="SHEIN API Error",
            detail=detail,
        )

    def _request_headers(self, api_path: str, body_str: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            SHEIN_APP_ID_HEADER: self.open_key_id,
            SHEIN_TIMESTAMP_HEADER: timestamp,
            SHEIN_REQUEST_SIGNATURE_HEADER: sign_shein_request(
                app_id=self.open_key_id,
                secret_key=self._secret_key,
                api_path=api_path,
                timestamp=timestamp,
                body_str=body_str,
            ),
        }

    async def _request(
        self,
        method: str,
        api_path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Sign and execute one Open Platform call, mapping failures to typed ``AppError``s."""
        body_str = (
            json.dumps(body, separators=(",", ":"), sort_keys=True) if body is not None else ""
        )
        upper_method = method.upper()
        url = f"{self.endpoint_url}{api_path}"
        headers = self._request_headers(api_path, body_str)
        try:
            async with self._client() as client:
                response = await client.request(
                    upper_method,
                    url,
                    params=dict(query) if query else None,
                    content=body_str or None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "shein_transport_error",
                seller_id=self.seller_id,
                api_path=api_path,
                method=upper_method,
                error=self._redact(str(exc))[:300],
            )
            raise AppError(
                slug="shein-api-error",
                status_code=502,
                code="ERR_SHEIN_API_ERROR",
                title="SHEIN API Error",
                detail=self._redact(
                    f"SHEIN Open Platform {upper_method} {api_path} failed before a response "
                    "was received; the request can be retried."
                ),
            ) from exc

        raw_text = self._redact(response.text)
        payload = self._parse_payload(response)
        self._raise_for_failure(
            response.status_code,
            payload,
            raw_text,
            method=upper_method,
            api_path=api_path,
        )
        return payload

    # ---------------------------------------------------------------- normalization

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        sku = _first_str(item, "skuCode", "sku_code", "goodsSn", "sku", "sellerSku", "goodsNo")
        if sku is None:
            logger.warning(
                "shein_order_line_missing_sku",
                seller_id=self.seller_id,
                goods_id=_first_str(item, "goodsId", "productId"),
            )
            return None
        quantity = max(
            1,
            _to_int(
                _lookup(item, "quantity", "qty", "goodsNum", "num", "skuQuantity"),
                what="line.quantity",
            ),
        )
        # Keep full per-unit precision: quantizing before multiplying drifts a 100-unit line.
        unit_price = _to_decimal(
            _lookup(item, "unitPrice", "price", "goodsPrice", "skuPrice"),
            what="line.unit_price",
        )
        explicit_total = _lookup(
            item, "lineTotal", "amount", "totalPrice", "goodsAmount", "payAmount"
        )
        line_total = (
            _money(explicit_total, what="line.line_total")
            if explicit_total is not None
            else (unit_price * Decimal(quantity)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=_first_str(item, "goodsTitle", "productName", "title", "goodsName") or sku,
            quantity=quantity,
            line_total=line_total,
        )

    @staticmethod
    def _fee_block(order: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("feeInfo", "fee_info", "settlement", "payment"):
            nested = order.get(key)
            if isinstance(nested, Mapping):
                return nested
        return {}

    def _payment_status(self, status: str, refunded: Decimal) -> PaymentStatus:
        if status in {"pending", "unpaid", "pending_payment", "1"}:
            return PaymentStatus.UNPAID
        if refunded > Decimal("0.00"):
            return PaymentStatus.REFUNDED
        if status in {"cancelled", "canceled", "refunded", "5", "6"}:
            return PaymentStatus.REFUNDED if status in {"refunded", "6"} else PaymentStatus.UNPAID
        return PaymentStatus.PAID

    def _normalize_order(
        self, order: Mapping[str, Any], *, fallback_updated_at: datetime
    ) -> NormalizedOrder | None:
        """Map one SHEIN order onto the shared normalized order DTO."""
        external_order_id = _first_str(order, "orderNo", "order_no", "orderId", "id")
        if external_order_id is None:
            logger.warning("shein_order_missing_id", seller_id=self.seller_id)
            return None
        status = (_first_str(order, "orderStatus", "order_status", "status") or "").lower()

        line_items = [
            line
            for row in self._rows(order, _ORDER_LINE_KEYS)
            if (line := self._normalize_line_item(row)) is not None
        ]
        if not line_items:
            logger.warning(
                "shein_order_has_no_resolvable_line_items",
                seller_id=self.seller_id,
                external_order_id=external_order_id,
            )
            return None

        subtotal_raw = _lookup(order, "goodsAmount", "subtotal", "orderAmount", "goodsAmountTotal")
        subtotal = (
            _money(subtotal_raw, what="subtotal")
            if subtotal_raw is not None
            else sum((line.line_total for line in line_items), Decimal("0.00")).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )
        shipping = _money_from(order, "shippingFee", "freight", "shippingAmount", "expressFee")
        discount = _money_from(
            order, "discountAmount", "discount", "sellerDiscount", "couponAmount"
        )
        tax = _money_from(order, "taxAmount", "tax", "taxCollected")
        total_raw = _lookup(order, "totalAmount", "orderAmount", "payAmount", "totalPayAmount")
        total = (
            _money(total_raw, what="total_amount")
            if total_raw is not None
            else (subtotal + shipping + tax - discount).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )

        fee_block = self._fee_block(order)
        commission = _money_from(order, *_COMMISSION_KEYS)
        if commission == Decimal("0.00") and fee_block:
            commission = _money_from(fee_block, *_COMMISSION_KEYS)
        refunded = _money_from(order, *_REFUND_KEYS)
        if refunded == Decimal("0.00") and fee_block:
            refunded = _money_from(fee_block, *_REFUND_KEYS)

        buyer_block = order.get("buyerInfo") or order.get("buyer")
        buyer: Mapping[str, Any] = buyer_block if isinstance(buyer_block, Mapping) else {}
        recipient_block = order.get("receiptAddress") or order.get("recipient")
        recipient: Mapping[str, Any] = (
            recipient_block if isinstance(recipient_block, Mapping) else {}
        )

        email = _first_str(order, "buyerEmail", "email", "contactEmail") or _first_str(
            buyer, "email", "buyerEmail"
        )
        phone = _first_str(order, "buyerPhone", "phone", "contactPhone") or _first_str(
            recipient, "phone", "phoneNumber", "mobile"
        )
        buyer_id = _first_str(order, "buyerId", "userId", "customerId") or _first_str(
            buyer, "buyerId", "userId"
        )
        display_name = (
            _first_str(order, "buyerName", "receiverName", "contactName")
            or _first_str(recipient, "name", "receiverName", "fullName")
            or email
            or "SHEIN Buyer"
        )
        country = _country_code(
            _first_str(order, "countryCode", "country", "regionCode")
            or _first_str(recipient, "countryCode", "country", "regionCode")
        )

        metadata: dict[str, Any] = {
            "shein_order_status": status,
            "shein_seller_id": self.seller_id,
            "shein_region": self.region,
            "shein_fulfillment_mode": self.fulfillment_mode,
        }
        parent_no = _first_str(order, "parentOrderNo", "parent_order_no")
        if parent_no:
            metadata["shein_parent_order_no"] = parent_no
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )

        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=_first_str(order, "orderNo", "order_no") or external_order_id,
            order_mode=OrderMode.B2C_RETAIL,
            currency=(
                _first_str(order, "currency", "orderCurrency", "currencyCode") or "USD"
            ).upper(),
            # Locked at ingestion by `resolve_order_fx_rate`; None means "resolve, never guess".
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP),
            fulfillment_status=ORDER_STATUS_MAP.get(status, FulfillmentStatus.CONFIRMED_RESERVED),
            payment_status=self._payment_status(status, refunded),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone,
                external_customer_id=buyer_id,
                external_identity_type=(
                    IdentityType.EMAIL
                    if email
                    else IdentityType.PHONE_E164
                    if phone
                    else IdentityType.WEB_VISITOR_ID
                ),
                display_name=display_name,
                country_code=country,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_shein_datetime(
                _lookup(
                    order,
                    "updateTime",
                    "update_time",
                    "modifyTime",
                    "gmtModified",
                    "orderUpdateTime",
                ),
                fallback_updated_at,
            ),
        )

    @staticmethod
    def _product_variants(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        raw_variants: list[Any] | None = None
        for key in _PRODUCT_VARIANT_KEYS:
            candidate = row.get(key)
            if isinstance(candidate, list):
                raw_variants = candidate
                break
        if raw_variants is None:
            return variants
        for raw in raw_variants:
            if not isinstance(raw, Mapping):
                continue
            price_raw = _lookup(raw, "price", "unitPrice", "skuPrice", "retailPrice")
            stock_raw = _lookup(raw, "quantity", "stock", "inventory", "availableStock")
            option_values: dict[str, str] = {}
            raw_attributes = raw.get("attributes") or raw.get("optionValues")
            if isinstance(raw_attributes, Mapping):
                option_values = {str(k): str(v) for k, v in raw_attributes.items()}
            elif isinstance(raw_attributes, list):
                for attribute in raw_attributes:
                    if not isinstance(attribute, Mapping):
                        continue
                    name = _first_str(attribute, "name", "attributeName")
                    value = _first_str(attribute, "value", "valueName")
                    if name and value:
                        option_values[name] = value
            variants.append(
                {
                    "external_sku": _first_str(
                        raw, "skuCode", "sku_code", "goodsSn", "sku", "sellerSku"
                    )
                    or "",
                    "external_variant_id": _first_str(raw, "skuId", "id", "variantId"),
                    "unit_price_usd": (
                        str(_money(price_raw, what="sku_price")) if price_raw is not None else None
                    ),
                    "stock_quantity": (
                        max(0, _to_int(stock_raw, what="sku_stock"))
                        if stock_raw is not None
                        else None
                    ),
                    "option_values": option_values,
                }
            )
        return variants

    # ------------------------------------------------------------------ adapter API

    async def _order_detail(self, order_no: str) -> dict[str, Any]:
        payload = await self._request("GET", SHEIN_ORDER_DETAIL_PATH, query={"orderNo": order_no})
        data = self._data_of(payload)
        for key in _ORDER_DETAIL_KEYS:
            nested = data.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
        return data

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._request(
            "POST",
            SHEIN_ORDER_LIST_PATH,
            body={
                # queryType 2 = "orders updated since startTime", SHEIN's incremental window.
                "queryType": 2,
                "startTime": int(window_start.timestamp() * 1000),
                "page": page,
                "pageSize": SHEIN_PAGE_SIZE,
            },
        )
        data = self._data_of(payload)
        rows = self._rows(data, _ORDER_ROW_KEYS)

        normalized: list[NormalizedOrder] = []
        for row in rows:
            order_no = _first_str(row, "orderNo", "order_no", "orderId", "id")
            merged: Mapping[str, Any] = row
            if order_no and not self._rows(row, _ORDER_LINE_KEYS):
                # The list endpoint only carries headers for some accounts: the lines that
                # make the order ingestible live behind the detail endpoint.
                merged = {**row, **await self._order_detail(order_no)}
            order = self._normalize_order(merged, fallback_updated_at=window_start)
            if order is not None:
                normalized.append(order)
        return normalized, _next_cursor(data, page, len(rows))

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._request(
            "POST",
            SHEIN_PRODUCT_LIST_PATH,
            body={
                "updateTimeStart": int(window_start.timestamp() * 1000),
                "page": page,
                "pageSize": SHEIN_PAGE_SIZE,
            },
        )
        data = self._data_of(payload)
        rows = self._rows(data, _PRODUCT_ROW_KEYS)
        products: list[NormalizedProduct] = []
        for row in rows:
            product_id = _first_str(row, "goodsId", "productId", "product_id", "goodsSn", "spuName")
            if product_id is None:
                continue
            products.append(
                NormalizedProduct(
                    external_product_id=product_id,
                    title=_first_str(row, "goodsTitle", "productName", "title") or product_id,
                    variants=self._product_variants(row),
                )
            )
        return products, _next_cursor(data, page, len(rows))

    def _delivery_type(self) -> str:
        return (
            SHEIN_SEMI_MANAGED_SHIPPING
            if self.fulfillment_mode == "semi_managed"
            else SHEIN_SELF_SHIPPING
        )

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        order_no = external_order_id.strip()
        tracking = tracking_number.strip()
        carrier = carrier_company.strip()
        if not order_no or not tracking or not carrier:
            raise AppError(
                slug="shein-fulfillment-rejected",
                status_code=422,
                code="ERR_SHEIN_FULFILLMENT_REJECTED",
                title="SHEIN Fulfillment Rejected",
                detail=(
                    "SHEIN shipping needs a non-empty order number, tracking number and carrier."
                ),
            )
        body: dict[str, Any] = {
            "orderNo": order_no,
            "deliveryType": self._delivery_type(),
            "expressCompany": carrier,
            "trackingNumber": tracking,
            "notifyCustomer": bool(notify_customer),
        }
        if tracking_url:
            body["trackingUrl"] = tracking_url.strip()
        if fulfillment_order_id:
            body["packageNo"] = fulfillment_order_id.strip()
        logger.info(
            "shein_order_ship",
            seller_id=self.seller_id,
            order_no=order_no,
            delivery_type=body["deliveryType"],
            package_no=fulfillment_order_id,
            notify_customer=notify_customer,
        )
        payload = await self._request("POST", SHEIN_ORDER_SHIP_PATH, body=body)
        return self._data_of(payload)

    async def _upload_image(self, blob: MarketplaceImageBlob) -> str | None:
        """Upload one binary image asset and return the URL SHEIN hosts it at."""
        api_path = SHEIN_PRODUCT_IMAGE_PATH
        body_str = ""
        headers = self._request_headers(api_path, body_str)
        headers.pop("Content-Type", None)
        filename = blob.filename.strip() or "product.jpg"
        mime_type = blob.mime_type.strip() or "image/jpeg"
        files = {"file": (filename, bytes(blob.data), mime_type)}
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.endpoint_url}{api_path}", files=files, headers=headers
                )
        except httpx.HTTPError as exc:
            raise AppError(
                slug="shein-media-upload-failed",
                status_code=502,
                code="ERR_SHEIN_MEDIA_UPLOAD",
                title="SHEIN Image Upload Failed",
                detail=self._redact(
                    f"SHEIN image upload for '{filename}' failed before a response was "
                    "received; the publish can be retried."
                ),
            ) from exc
        payload = self._parse_payload(response)
        self._raise_for_failure(
            response.status_code,
            payload,
            self._redact(response.text),
            method="POST",
            api_path=api_path,
        )
        data = self._data_of(payload)
        url = _first_str(data, "imageUrl", "url", "image_url", "httpsUrl")
        if url is None:
            raise AppError(
                slug="shein-media-upload-failed",
                status_code=502,
                code="ERR_SHEIN_MEDIA_UPLOAD",
                title="SHEIN Image Upload Failed",
                detail=self._redact(
                    f"SHEIN accepted image '{filename}' but returned no hosted URL."
                ),
            )
        return url

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create one single- or multi-variant SHEIN listing from the catalog DTO."""
        clean_sku = payload.external_sku.strip()
        clean_title = payload.title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="shein-listing-rejected",
                status_code=422,
                code="ERR_SHEIN_LISTING_REJECTED",
                title="SHEIN Listing Rejected",
                detail="A SHEIN listing requires a non-empty SKU and title.",
            )
        base_price = _money(payload.price_usd, what="price_usd")

        gallery: list[str] = []
        for blob in payload.image_blobs:
            hosted = await self._upload_image(blob)
            if hosted:
                gallery.append(hosted)
        for url in payload.images:
            clean_url = (url or "").strip()
            if clean_url.startswith("https://"):
                gallery.append(clean_url)

        sku_entries: list[dict[str, Any]] = []
        for variant in payload.variants:
            variant_sku = variant.external_sku.strip() or clean_sku
            variant_price = _money(variant.price_usd, what="variant.price_usd")
            entry: dict[str, Any] = {
                "skuCode": variant_sku,
                "price": str(variant_price),
                "stock": max(0, int(variant.quantity)),
            }
            attributes = [
                {"name": str(name), "value": str(value)}
                for name, value in variant.option_values.items()
            ]
            if attributes:
                entry["attributes"] = attributes
            if variant.image_url:
                entry["imageUrl"] = variant.image_url.strip()
            sku_entries.append(entry)
        if not sku_entries:
            sku_entries.append(
                {
                    "skuCode": clean_sku,
                    "price": str(base_price),
                    "stock": max(0, int(payload.quantity)),
                }
            )

        options_payload = [
            {"name": option.name, "values": [str(value) for value in option.values]}
            for option in payload.options
        ]
        price_ladder = [
            {
                "minQty": tier.min_qty,
                "unitPrice": str(_money(tier.unit_price_usd, what="moq_tier.unit_price_usd")),
            }
            for tier in payload.moq_tiers
        ]

        body: dict[str, Any] = {
            "productName": clean_title[:255],
            "description": _listing_description(payload.description, payload.bullet_points)[:10000],
            "categoryName": payload.category.strip(),
            "currency": "USD",
            "region": self.region,
            "skuList": sku_entries,
            "imageUrls": gallery[:9],
            "options": options_payload,
            "priceLadder": price_ladder,
        }
        if payload.hs_code:
            body["hsCode"] = payload.hs_code.strip()

        response = await self._request("POST", SHEIN_PRODUCT_CREATE_PATH, body=body)
        data = self._data_of(response)
        product_id = _extract_product_id(data)
        if product_id is None:
            raise AppError(
                slug="shein-listing-rejected",
                status_code=502,
                code="ERR_SHEIN_LISTING_REJECTED",
                title="SHEIN Listing Rejected",
                detail=self._redact(
                    f"SHEIN accepted the listing request but returned no product id for "
                    f"SKU '{clean_sku}'."
                ),
            )

        receipts: list[MarketplaceVariantReceipt] = []
        for row in self._rows(data, _PRODUCT_VARIANT_KEYS):
            receipts.append(
                MarketplaceVariantReceipt(
                    external_sku=_first_str(row, "skuCode", "sku_code", "goodsSn", "sku")
                    or clean_sku,
                    external_variant_id=_first_str(row, "skuId", "id", "variantId"),
                    external_inventory_item_id=_first_str(
                        row, "inventoryId", "warehouseId", "inventory_id"
                    ),
                )
            )
        if not receipts:
            receipts = [
                MarketplaceVariantReceipt(external_sku=str(entry["skuCode"]))
                for entry in sku_entries
            ]
        return MarketplaceListingDispatchReceipt(
            external_product_id=product_id,
            external_sku=clean_sku,
            status="PUBLISHED",
            raw_response=dict(response),
            variant_receipts=receipts,
        )

    async def _resolve_sku(self, external_sku: str) -> tuple[str | None, str | None]:
        """Resolve a SHEIN ``goodsId``/``skuId`` pair for one seller SKU."""
        payload = await self._request(
            "POST",
            SHEIN_PRODUCT_LIST_PATH,
            body={"skuCodeList": [external_sku], "page": 1, "pageSize": 20},
        )
        data = self._data_of(payload)
        for product in self._rows(data, _PRODUCT_ROW_KEYS):
            product_id = _first_str(
                product, "goodsId", "productId", "product_id", "goodsSn", "spuName"
            )
            for variant in self._rows(product, _PRODUCT_VARIANT_KEYS):
                variant_sku = _first_str(
                    variant, "skuCode", "sku_code", "goodsSn", "sku", "sellerSku"
                )
                if variant_sku != external_sku:
                    continue
                return product_id, _first_str(variant, "skuId", "id", "variantId")
            if product_id is not None:
                return product_id, None
        return None, None

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
        """Push one absolute on-hand quantity to SHEIN (never a delta, never negative)."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="shein-inventory-sync-failed",
                status_code=422,
                code="ERR_SHEIN_INVENTORY_SYNC",
                title="SHEIN Inventory Sync Failed",
                detail="SHEIN inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        product_id = (external_product_id or "").strip() or None
        variant_id = (external_variant_id or "").strip() or None
        if product_id is None:
            product_id, resolved_variant = await self._resolve_sku(clean_sku)
            variant_id = variant_id or resolved_variant
        if product_id is None:
            raise AppError(
                slug="shein-inventory-sync-failed",
                status_code=502,
                code="ERR_SHEIN_INVENTORY_SYNC",
                title="SHEIN Inventory Sync Failed",
                detail=self._redact(f"Could not resolve a SHEIN goodsId for SKU '{clean_sku}'."),
            )
        inv_item_id = (external_inventory_item_id or "").strip() or None
        loc_id = (location_id or "").strip() or None
        wh_id = (warehouse_id or "").strip() or None

        body: dict[str, Any] = {
            "goodsId": product_id,
            "skuCode": clean_sku,
            "quantity": norm_qty,
        }
        if variant_id:
            body["skuId"] = variant_id
        if inv_item_id:
            body["inventoryId"] = inv_item_id
        if wh_id:
            body["warehouseId"] = wh_id
        if loc_id:
            body["locationId"] = loc_id

        response = await self._request("POST", SHEIN_INVENTORY_UPDATE_PATH, body=body)
        return {
            "channel": "shein",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_product_id": product_id,
            "external_variant_id": variant_id,
            "external_inventory_item_id": inv_item_id,
            "location_id": loc_id,
            "warehouse_id": wh_id,
            "raw": self._data_of(response),
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shein_webhook(raw_body, headers, secret or self._secret_key)
