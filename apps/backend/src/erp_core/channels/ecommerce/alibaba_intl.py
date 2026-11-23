"""Alibaba.com International Open Platform (IOP) connector: B2B trade, ICBU, webhooks.

Signing follows the Alibaba IOP canonical form: the request path followed by every signed
parameter as ``key + value`` in ascending key order, HMAC-SHA256 with the app secret and
upper-case hex output. The secret itself is never transmitted, logged or echoed back in an
``AppError``; monetary math is always :class:`decimal.Decimal` with ``ROUND_HALF_UP``.
"""

from __future__ import annotations

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
from pydantic import BaseModel, Field

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

logger = structlog.get_logger(__name__)

ALIBABA_IOP_DEFAULT_BASE_URL = "https://openapi-api.alibaba.com/rest"
ALIBABA_IOP_ORDER_LIST_PATH = "/alibaba/trade/order/list/get"
ALIBABA_IOP_PRODUCT_LIST_PATH = "/alibaba/icbu/product/list/get"
ALIBABA_IOP_PRODUCT_ADD_PATH = "/alibaba/icbu/product/add"
ALIBABA_IOP_INVENTORY_UPDATE_PATH = "/alibaba/icbu/product/inventory/update"
ALIBABA_IOP_SHIPPING_SEND_PATH = "/alibaba/trade/logistics/shipping/send"
ALIBABA_SIGNATURE_HEADER = "X-Alibaba-Signature"

#: Alibaba IOP pages are fixed at 50 rows; the cursor is a 1-based page number.
ALIBABA_IOP_PAGE_SIZE = 50
#: All money lands in NUMERIC(…, 2) columns; quantize once, half-up, never from floats.
MONEY_SCALE = Decimal("0.01")
#: ``sign`` is the only parameter excluded from its own canonical payload.
_UNSIGNED_PARAMS = frozenset({"sign"})
_SUCCESS_CODES = frozenset({"0", "200", "success", "true"})
_CANCELLED_STATUSES = frozenset({"cancel", "cancelled", "terminated"})

ORDER_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "waitbuyerpay": FulfillmentStatus.PENDING_PAYMENT,
    "waitsellersend": FulfillmentStatus.CONFIRMED_RESERVED,
    "waitbuyerreceive": FulfillmentStatus.SHIPPED,
    "success": FulfillmentStatus.DELIVERED,
    "cancel": FulfillmentStatus.CANCELLED,
    "cancelled": FulfillmentStatus.CANCELLED,
    "terminated": FulfillmentStatus.CANCELLED,
}

_ORDER_ROW_KEYS = ("orders", "order_list", "trade_list", "orders_list")
_ORDER_LINE_KEYS = ("products", "product_list", "order_products", "line_items")
_PRODUCT_ROW_KEYS = ("products", "product_list", "productList")


def sign_alibaba_iop_request(
    api_path: str, params: Mapping[str, str], app_secret: str
) -> str:
    """Canonical Alibaba IOP signature: ``HmacSHA256(secret, path + sorted(k + v))``, upper hex.

    ``sign`` is skipped so the function is idempotent on an already-signed parameter map,
    which is what lets webhook/API consumers re-verify a request byte for byte.
    """
    if not app_secret:
        raise ValueError("app_secret is required to sign Alibaba IOP requests")
    canonical = api_path + "".join(
        f"{key}{value}"
        for key, value in sorted(params.items())
        if key not in _UNSIGNED_PARAMS
    )
    return hmac.new(
        app_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()


def verify_alibaba_iop_webhook_signature(
    raw_body: bytes, headers: Mapping[str, str], secret: str
) -> bool:
    """Fail-closed, constant-time verification of ``X-Alibaba-Signature`` over the raw body."""
    received = _header(headers, ALIBABA_SIGNATURE_HEADER)
    if not secret or not received:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, received.strip().lower())
    except (TypeError, ValueError):
        return False


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (IOP partners send mixed-cased header names)."""
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
    """Parse an IOP numeric field without ever letting a binary float into money math."""
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
            slug="alibaba-api-error",
            status_code=502,
            code="ERR_ALIBABA_API_ERROR",
            title="Alibaba IOP API Error",
            detail=f"Alibaba IOP response carried an unparseable '{what}' value.",
        ) from exc


def _money(value: Any, *, what: str = "amount") -> Decimal:
    return _to_decimal(value, what=what).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _sum_money(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal("0.00")).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _to_int(value: Any, *, what: str) -> int:
    return int(_to_decimal(value, what=what))


def _iop_param(value: Any) -> str:
    """Flatten a parameter value into the exact string that gets signed and sent."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return ""
    return str(value)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iop_datetime(value: Any, fallback: datetime) -> datetime:
    """Parse IOP timestamps (``YYYY-MM-DD HH:MM:SS``, ISO-8601 or epoch millis) as UTC."""
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
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            return fallback
    return _as_utc(parsed)


def _country_code(value: Any, default: str = "US") -> str:
    text = str(value or "").strip().upper()
    return text if len(text) == 2 and text.isalpha() else default


def _page_number(cursor: str | None) -> int:
    if cursor is None or not str(cursor).strip():
        return 1
    text = str(cursor).strip()
    if not text.isdigit() or int(text) < 1:
        raise AppError(
            slug="alibaba-invalid-cursor",
            status_code=422,
            code="ERR_ALIBABA_INVALID_CURSOR",
            title="Invalid Alibaba Cursor",
            detail="Alibaba IOP cursors are 1-based page numbers.",
        )
    return int(text)


def _next_cursor(data: Mapping[str, Any], page: int) -> str | None:
    """Advance only on an explicit ``has_next`` or a total that covers a further page."""
    has_next = data.get("has_next")
    if has_next is not None and str(has_next).strip().lower() in {"true", "1", "yes"}:
        return str(page + 1)
    total_raw = _lookup(data, "total_count", "total", "total_num")
    if total_raw is not None:
        try:
            total = int(str(total_raw).strip())
        except ValueError:
            return None
        if total > page * ALIBABA_IOP_PAGE_SIZE:
            return str(page + 1)
    return None


def _listing_description(description: str, bullet_points: Sequence[str]) -> str:
    body = str(description).strip()
    bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
    if not bullets:
        return body
    bullet_block = "\n".join(f"- {bullet}" for bullet in bullets)
    return "\n\n".join(part for part in (body, bullet_block) if part)


def _normalize_moq_tiers(tiers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize MOQ ladders to ``{"min_qty": int, "unit_price_usd": "<exact decimal>"}``."""
    normalized: list[dict[str, Any]] = []
    for tier in tiers:
        min_qty_raw = _lookup(tier, "min_qty", "min_quantity", "quantity", "start_quantity")
        price_raw = _lookup(tier, "unit_price_usd", "price_usd", "unit_price", "price")
        if min_qty_raw is None or price_raw is None:
            raise AppError(
                slug="alibaba-invalid-moq-tier",
                status_code=422,
                code="ERR_ALIBABA_INVALID_MOQ_TIER",
                title="Invalid MOQ Tier",
                detail="Every MOQ tier needs a minimum quantity and a unit price.",
            )
        min_qty = _to_int(min_qty_raw, what="moq_tier.min_qty")
        if min_qty <= 0:
            raise AppError(
                slug="alibaba-invalid-moq-tier",
                status_code=422,
                code="ERR_ALIBABA_INVALID_MOQ_TIER",
                title="Invalid MOQ Tier",
                detail="MOQ tier minimum quantities must be greater than zero.",
            )
        normalized.append(
            {
                "min_qty": min_qty,
                "unit_price_usd": str(_money(price_raw, what="moq_tier.unit_price_usd")),
            }
        )
    return sorted(normalized, key=lambda tier: int(tier["min_qty"]))


def _extract_product_id(payload: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = [payload]
    result = payload.get("result")
    if isinstance(result, Mapping):
        candidates.append(result)
        candidates.append(result.get("data"))
    candidates.append(payload.get("data"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        value = _lookup(candidate, "product_id", "productId")
        if value is not None:
            return str(value).strip() or None
    return None


class AlibabaListingReceipt(BaseModel):
    """Proof that ICBU accepted a product and the external ids the ERP must persist."""

    external_product_id: str
    external_sku: str
    status: str = "PUBLISHED"
    raw_response: dict[str, Any] = Field(default_factory=dict)


class AlibabaIntlAdapter(CommercePlatformAdapter):
    """Alibaba.com International Open Platform adapter (Trade Assurance + ICBU)."""

    def __init__(
        self,
        *,
        store_id: str,
        app_key: str,
        app_secret: str,
        access_token: str | None = None,
        api_base_url: str = ALIBABA_IOP_DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not app_key or not app_secret:
            raise ValueError("app_key and app_secret are required for the Alibaba IOP adapter")
        self.store_id = store_id
        self.app_key = app_key
        self._app_secret = app_secret
        self._access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout

    # ------------------------------------------------------------------ transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=self.timeout)

    def _redact(self, text: str) -> str:
        """Strip every credential the adapter holds out of anything user-visible."""
        cleaned = text
        for secret in (self._app_secret, self._access_token or ""):
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
                    _first_str(
                        source,
                        "msg",
                        "message",
                        "error_message",
                        "errorMessage",
                        "sub_msg",
                    )
                    or ""
                )
            if code or message:
                break
        return code, message

    def _raise_for_iop_failure(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        raw_text: str,
        *,
        method: str,
        api_path: str,
    ) -> None:
        code, message = self._error_signals(payload)
        result = payload.get("result")
        failed = (
            status_code >= 400
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
                "illegalaccesstoken",
                "signature",
                "accesstoken",
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
            f"Alibaba IOP {method} {api_path} failed (http_status={status_code}, "
            f"code={code or 'unknown'}): {reason}"
        )[:500]
        logger.warning(
            "alibaba_iop_request_failed",
            store_id=self.store_id,
            api_path=api_path,
            http_status=status_code,
            iop_code=code,
            auth_failed=auth_failed,
            rate_limited=rate_limited,
        )
        if auth_failed:
            raise AppError(
                slug="alibaba-auth-failed",
                status_code=502,
                code="ERR_ALIBABA_AUTH_FAILED",
                title="Alibaba IOP Authentication Failed",
                detail=detail,
            )
        if rate_limited:
            raise AppError(
                slug="alibaba-rate-limited",
                status_code=429,
                code="ERR_ALIBABA_RATE_LIMITED",
                title="Alibaba IOP Rate Limited",
                detail=detail,
            )
        raise AppError(
            slug="alibaba-api-error",
            status_code=502,
            code="ERR_ALIBABA_API_ERROR",
            title="Alibaba IOP API Error",
            detail=detail,
        )

    async def _call_iop(
        self, method: str, api_path: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Sign and execute one IOP call, mapping the IOP envelope to typed ``AppError``s."""
        signed: dict[str, str] = {
            "app_key": self.app_key,
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),
        }
        if self._access_token:
            signed["session"] = self._access_token
        for key, value in params.items():
            if key in _UNSIGNED_PARAMS:
                continue
            signed[key] = _iop_param(value)
        signed["sign"] = sign_alibaba_iop_request(api_path, signed, self._app_secret)

        upper_method = method.upper()
        url = f"{self.api_base_url}{api_path}"
        headers = {"Accept": "application/json"}
        try:
            async with self._client() as client:
                if upper_method in {"GET", "DELETE"}:
                    response = await client.request(
                        upper_method, url, params=signed, headers=headers
                    )
                else:
                    response = await client.request(
                        upper_method, url, data=signed, headers=headers
                    )
        except httpx.HTTPError as exc:
            logger.warning(
                "alibaba_iop_transport_error",
                store_id=self.store_id,
                api_path=api_path,
                method=upper_method,
                error=self._redact(str(exc))[:300],
            )
            raise AppError(
                slug="alibaba-api-error",
                status_code=502,
                code="ERR_ALIBABA_API_ERROR",
                title="Alibaba IOP API Error",
                detail=self._redact(
                    f"Alibaba IOP {upper_method} {api_path} failed before a response was "
                    "received; the request can be retried."
                ),
            ) from exc

        raw_text = self._redact(response.text)
        payload = self._parse_payload(response)
        self._raise_for_iop_failure(
            response.status_code,
            payload,
            raw_text,
            method=upper_method,
            api_path=api_path,
        )
        return payload

    # -------------------------------------------------------------- payload helpers

    @staticmethod
    def _data_of(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Unwrap the IOP envelope: ``result.data`` -> ``data`` -> the payload itself."""
        result = payload.get("result")
        if isinstance(result, Mapping):
            nested = result.get("data")
            if isinstance(nested, Mapping):
                return dict(nested)
        nested = payload.get("data")
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

    # ---------------------------------------------------------------- normalization

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        sku = _first_str(item, "sku_code", "sku", "product_sku", "sku_id", "product_id")
        if sku is None:
            logger.warning(
                "alibaba_iop_line_item_missing_sku",
                store_id=self.store_id,
                product_id=_first_str(item, "product_id"),
            )
            return None
        quantity = max(
            1, _to_int(_lookup(item, "quantity", "product_quantity", "num"), what="quantity")
        )
        # Keep the full per-unit precision: quantizing the unit price before multiplying
        # would drift a 100-unit wholesale line by real money.
        unit_price = _to_decimal(
            _lookup(item, "unit_price", "price", "sku_price"), what="unit_price"
        )
        explicit_total = _lookup(
            item, "amount", "total_price", "product_amount", "line_total"
        )
        line_total = (
            _money(explicit_total, what="line_total")
            if explicit_total is not None
            else (unit_price * Decimal(quantity)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=_first_str(item, "product_name", "subject", "title") or sku,
            quantity=quantity,
            line_total=line_total,
        )

    @staticmethod
    def _payment_status(status: str, order: Mapping[str, Any]) -> PaymentStatus:
        if status == "waitbuyerpay":
            return PaymentStatus.UNPAID
        paid_evidence = _lookup(
            order, "gmt_pay", "pay_time", "gmt_payment", "paid_time", "paid"
        )
        if status in _CANCELLED_STATUSES:
            return PaymentStatus.REFUNDED if paid_evidence is not None else PaymentStatus.UNPAID
        return PaymentStatus.PAID

    def _normalize_order(
        self, order: Mapping[str, Any], *, fallback_updated_at: datetime
    ) -> NormalizedOrder | None:
        """Map one Trade Assurance / Wholesale trade onto the shared B2B order DTO."""
        external_order_id = _first_str(order, "trade_id", "order_id", "id")
        if external_order_id is None:
            logger.warning("alibaba_iop_order_missing_id", store_id=self.store_id)
            return None
        status = (_first_str(order, "status", "order_status", "trade_status") or "").lower()
        buyer_block = order.get("buyer")
        buyer: Mapping[str, Any] = buyer_block if isinstance(buyer_block, Mapping) else {}

        line_items = [
            item
            for row in self._rows(order, _ORDER_LINE_KEYS)
            if (item := self._normalize_line_item(row)) is not None
        ]

        subtotal_raw = _lookup(
            order, "subtotal", "product_amount", "product_total", "goods_amount"
        )
        subtotal = (
            _money(subtotal_raw, what="subtotal")
            if subtotal_raw is not None
            else _sum_money(line.line_total for line in line_items)
        )
        shipping = _money(
            _lookup(order, "shipping_fee", "freight_amount", "logistics_fee"),
            what="shipping_fee",
        )
        discount = _money(
            _lookup(order, "discount_amount", "discount", "seller_discount"),
            what="discount_amount",
        )
        tax = _money(
            _lookup(order, "tax_amount", "tax", "tax_collected"), what="tax_amount"
        )
        total_raw = _lookup(order, "total_amount", "order_amount", "total")
        total = (
            _money(total_raw, what="total_amount")
            if total_raw is not None
            else (subtotal + shipping + tax - discount).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        )
        commission = _money(
            _lookup(order, "platform_commission", "commission_amount"),
            what="platform_commission",
        )

        country = _country_code(
            _first_str(order, "buyer_country", "country_code", "country")
            or _first_str(buyer, "country_code", "country")
        )
        company_name = _first_str(
            order, "buyer_company_name", "company_name", "buyer_corporation_name"
        ) or _first_str(buyer, "company_name")
        display_name = (
            company_name
            or _first_str(order, "buyer_email", "buyer_login_id", "buyer_name")
            or _first_str(buyer, "display_name", "name")
            or "Alibaba Buyer"
        )

        metadata: dict[str, Any] = {
            "alibaba_platform": "alibaba_com_intl",
            "alibaba_trade_status": status,
            "alibaba_store_id": self.store_id,
        }
        order_number = _first_str(order, "order_no", "trade_no")
        if order_number:
            metadata["alibaba_order_no"] = order_number
        buyer_login = _first_str(order, "buyer_login_id", "buyer_id")
        if buyer_login:
            metadata["alibaba_buyer_login_id"] = buyer_login

        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=order_number,
            order_mode=OrderMode.B2B_WHOLESALE,
            currency=(
                _first_str(order, "currency", "order_currency", "currency_code") or "USD"
            ).upper(),
            # Wholesale trades are locked to the tenant base rate at ingestion; None means
            # "resolve it honestly from live quotes", never guess.
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission,
            fulfillment_status=ORDER_STATUS_MAP.get(
                status, FulfillmentStatus.CONFIRMED_RESERVED
            ),
            payment_status=self._payment_status(status, order),
            customer=NormalizedCustomer(
                email=_first_str(order, "buyer_email", "email") or _first_str(buyer, "email"),
                phone_e164=_first_str(order, "buyer_phone", "phone")
                or _first_str(buyer, "phone"),
                external_customer_id=buyer_login,
                display_name=display_name,
                country_code=country,
            ),
            b2b_company=NormalizedB2BCompany(
                legal_name=company_name or display_name,
                country_code=country,
                external_company_id=_first_str(
                    order, "buyer_company_id", "company_id"
                )
                or _first_str(buyer, "company_id"),
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_iop_datetime(
                _lookup(
                    order, "gmt_modified", "modified_time", "update_time", "gmt_update"
                ),
                fallback_updated_at,
            ),
        )

    @staticmethod
    def _product_variants(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_skus = _lookup(row, "skus", "sku_list", "skuList", "variants")
        if not isinstance(raw_skus, list):
            return []
        variants: list[dict[str, Any]] = []
        for sku in raw_skus:
            if not isinstance(sku, Mapping):
                continue
            price_raw = _lookup(sku, "price", "fob_price", "sku_price", "unit_price")
            quantity_raw = _lookup(sku, "quantity", "stock_quantity", "amount", "stock")
            variants.append(
                {
                    "external_sku": _first_str(
                        sku, "sku_code", "sku", "sku_id", "code"
                    )
                    or "",
                    "unit_price_usd": (
                        str(_money(price_raw, what="sku_price"))
                        if price_raw is not None
                        else None
                    ),
                    "stock_quantity": (
                        max(0, _to_int(quantity_raw, what="sku_stock"))
                        if quantity_raw is not None
                        else None
                    ),
                }
            )
        return variants

    # ------------------------------------------------------------------ adapter API

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._call_iop(
            "POST",
            ALIBABA_IOP_ORDER_LIST_PATH,
            {
                "gmt_modified_start": window_start.strftime("%Y-%m-%d %H:%M:%S"),
                "page": str(page),
                "page_size": str(ALIBABA_IOP_PAGE_SIZE),
            },
        )
        data = self._data_of(payload)
        orders = [
            normalized
            for row in self._rows(data, _ORDER_ROW_KEYS)
            if (normalized := self._normalize_order(row, fallback_updated_at=window_start))
            is not None
        ]
        return orders, _next_cursor(data, page)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = _page_number(cursor)
        window_start = _as_utc(since)
        payload = await self._call_iop(
            "POST",
            ALIBABA_IOP_PRODUCT_LIST_PATH,
            {
                "gmt_modified_start": window_start.strftime("%Y-%m-%d %H:%M:%S"),
                "page": str(page),
                "page_size": str(ALIBABA_IOP_PAGE_SIZE),
            },
        )
        data = self._data_of(payload)
        products: list[NormalizedProduct] = []
        for row in self._rows(data, _PRODUCT_ROW_KEYS):
            product_id = _first_str(row, "product_id", "id")
            if product_id is None:
                continue
            products.append(
                NormalizedProduct(
                    external_product_id=product_id,
                    title=_first_str(row, "subject", "title", "product_name") or product_id,
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
        params: dict[str, Any] = {
            "trade_id": external_order_id,
            "logistics_company": carrier_company,
            "tracking_no": tracking_number,
        }
        if tracking_url:
            params["tracking_url"] = tracking_url
        logger.info(
            "alibaba_iop_shipping_send",
            store_id=self.store_id,
            trade_id=external_order_id,
            delivery_order_id=fulfillment_order_id,
            notify_customer=notify_customer,
        )
        payload = await self._call_iop("POST", ALIBABA_IOP_SHIPPING_SEND_PATH, params)
        return self._data_of(payload)

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        return verify_alibaba_iop_webhook_signature(raw_body, headers, secret)

    # ------------------------------------------------- live product / inventory APIs

    async def publish_listing(
        self,
        *,
        sku: str,
        title: str,
        description: str,
        bullet_points: Sequence[str],
        price_usd: Decimal,
        quantity: int,
        images: Sequence[str],
        currency: str = "USD",
        category_id: str = "390501",
        category: str | None = None,
        hs_code: str | None = None,
        moq_tiers: Sequence[Mapping[str, Any]] = (),
        options: Sequence[Mapping[str, Any]] | None = None,
        variants: Sequence[Mapping[str, Any]] | None = None,
        image_blobs: Sequence[Mapping[str, Any]] | None = None,
    ) -> AlibabaListingReceipt:
        """Publish one ICBU product and return the external product id ICBU assigned."""
        del options, variants, image_blobs
        clean_sku = sku.strip()
        if not clean_sku:
            raise AppError(
                slug="alibaba-listing-invalid-sku",
                status_code=422,
                code="ERR_ALIBABA_LISTING_INVALID_SKU",
                title="Invalid Listing SKU",
                detail="An Alibaba listing needs a non-empty internal SKU.",
            )
        price = _money(price_usd, what="fob_price")
        params: dict[str, Any] = {
            "subject": title.strip()[:128],
            "sku_code": clean_sku,
            "category_id": category_id,
            "description": _listing_description(description, bullet_points),
            "image_urls": json.dumps([str(image) for image in images], separators=(",", ":")),
            "fob_price": str(price),
            "currency": (currency.strip().upper() or "USD"),
            "stock_quantity": str(max(1, int(quantity))),
            "hs_code": hs_code or "",
            "wholesale_ladder": json.dumps(
                _normalize_moq_tiers(moq_tiers), separators=(",", ":")
            ),
        }
        if category:
            params["category_name"] = str(category)
        payload = await self._call_iop("POST", ALIBABA_IOP_PRODUCT_ADD_PATH, params)
        product_id = _extract_product_id(payload)
        if product_id is None:
            raise AppError(
                slug="alibaba-listing-rejected",
                status_code=502,
                code="ERR_ALIBABA_LISTING_REJECTED",
                title="Alibaba Listing Rejected",
                detail=self._redact(
                    "Alibaba IOP accepted the product request but returned no product_id "
                    f"for SKU '{clean_sku}'."
                ),
            )
        return AlibabaListingReceipt(
            external_product_id=product_id,
            external_sku=clean_sku,
            status="PUBLISHED",
            raw_response=payload,
        )

    async def push_inventory_level(
        self, *, external_sku: str, quantity: int
    ) -> dict[str, Any]:
        payload = await self._call_iop(
            "POST",
            ALIBABA_IOP_INVENTORY_UPDATE_PATH,
            {
                "sku_code": external_sku.strip(),
                "stock_quantity": str(max(0, int(quantity))),
            },
        )
        return self._data_of(payload)
