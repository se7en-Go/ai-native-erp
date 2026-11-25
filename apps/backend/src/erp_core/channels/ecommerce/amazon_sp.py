"""Amazon Selling Partner API (SP-API) connector: LWA auth, orders, listings, inventory.

Money is parsed with :class:`decimal.Decimal` only. ``float`` never touches a price here:
``OrderTotal``, ``ItemPrice``, ``ShippingPrice``, ``ItemTax`` and ``PromotionDiscount`` are
converted straight from the JSON string SP-API sends into ``Decimal`` and quantized
``ROUND_HALF_UP`` to the currency's 2dp scale.

Credentials never leak: LWA / SP-API failures raise :class:`AppError` whose ``detail`` is
built from a whitelisted status/issue summary, then run through :meth:`_scrub`, which
replaces every known secret (client secret, refresh token, access token) with ``"***"``.
The request bodies that carry those secrets are never echoed into an error.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

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

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SP_API_NA_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
SP_API_EU_ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"
SP_API_FE_ENDPOINT = "https://sellingpartnerapi-fe.amazon.com"

#: North America marketplaces (US, CA, MX, BR).
NA_MARKETPLACE_IDS: frozenset[str] = frozenset(
    {"ATVPDKIKX0DER", "A2EUQ1WTGCTBG2", "A1AM78C64UM0Y8", "A2Q3Y263D00KWC"}
)
#: Europe / Middle East / India marketplaces.
EU_MARKETPLACE_IDS: frozenset[str] = frozenset(
    {
        "A1F83G8C2ARO7P",
        "A1PA6795UKMFR9",
        "A13V1IB3VIYZZH",
        "APJ6JRA9NG5V4",
        "A1RKKUPIHCS9HS",
        "A1805IZSGTT6HS",
        "A2NODRKZP88ZB9",
        "A1C3SOZRARQ6R3",
        "A33AVAJ2PDY3EV",
        "A2VIGQ35RCS4UG",
        "A17E79C6D8DWNP",
        "A21TJRUUN4KGV",
    }
)
#: Far East marketplaces (JP, AU, SG).
FE_MARKETPLACE_IDS: frozenset[str] = frozenset(
    {"A1VC38T7YXB528", "A39IBJ37TRP1C6", "A19VAU5U5O7RUS"}
)

_MONEY_SCALE = Decimal("0.01")

#: SP-API ``OrderStatus`` -> ERP fulfillment status. Unknown tokens stay reserved, never
#: shipped: an unmapped status must not silently release or deduct stock.
ORDER_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "Pending": FulfillmentStatus.PENDING_PAYMENT,
    "PendingAvailability": FulfillmentStatus.PENDING_PAYMENT,
    "Unshipped": FulfillmentStatus.CONFIRMED_RESERVED,
    "PartiallyShipped": FulfillmentStatus.PARTIALLY_SHIPPED,
    "InvoiceUnconfirmed": FulfillmentStatus.CONFIRMED_RESERVED,
    "Shipped": FulfillmentStatus.SHIPPED,
    "Canceled": FulfillmentStatus.CANCELLED,
    "Cancelled": FulfillmentStatus.CANCELLED,
    "Refunded": FulfillmentStatus.REFUNDED,
}

_ORDER_STATUS_MAP_LOWER: Mapping[str, FulfillmentStatus] = {
    k.lower(): v for k, v in ORDER_STATUS_MAP.items()
}
_CANCELED_STATUSES = frozenset({"canceled", "cancelled"})
_UNPAID_STATUSES = frozenset({"pending", "pendingavailability"})

_ORDER_ITEM_PAGE_LIMIT = 20


def amazon_sp_endpoint(marketplace_id: str) -> str:
    """Resolve the regional SP-API host for a marketplace id (default: North America)."""
    marketplace = marketplace_id.strip().upper()
    if marketplace in EU_MARKETPLACE_IDS:
        return SP_API_EU_ENDPOINT
    if marketplace in FE_MARKETPLACE_IDS:
        return SP_API_FE_ENDPOINT
    return SP_API_NA_ENDPOINT


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; SP-API only speaks absolute instants."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso8601_z(value: datetime) -> str:
    """SP-API timestamp format, e.g. ``2026-11-20T00:00:00.000Z``."""
    utc = _as_utc(value)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def _parse_iso8601(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _header_value(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (HTTP header names are not case sensitive)."""
    direct = headers.get(name)
    if direct:
        return str(direct)
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return ""


def _money_str(value: Decimal) -> str:
    """Serialize money for SP-API: fixed 2dp, half-up, never scientific notation."""
    return str(Decimal(value).quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP))


class AmazonListingReceipt(BaseModel):
    """Result of one Listings Items write (listing publish)."""

    external_product_id: str
    external_sku: str
    submission_id: str | None = None
    status: str
    issues: list[dict[str, Any]] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


class AmazonSpApiAdapter(CommercePlatformAdapter):
    """Amazon Selling Partner API adapter (LWA refresh-token auth, no AWS SigV4)."""

    def __init__(
        self,
        *,
        seller_id: str,
        marketplace_id: str = "ATVPDKIKX0DER",
        lwa_client_id: str,
        lwa_client_secret: str,
        refresh_token: str,
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.seller_id = seller_id.strip()
        self.marketplace_id = marketplace_id.strip().upper() or "ATVPDKIKX0DER"
        self.lwa_client_id = lwa_client_id
        self.lwa_client_secret = lwa_client_secret
        self.refresh_token = refresh_token
        self.endpoint = (endpoint_url or amazon_sp_endpoint(self.marketplace_id)).rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._access_token: str | None = None
        #: ``time.monotonic()`` deadline (already reduced by the 60s safety buffer).
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        async with self._client() as client:
            return await client.request(
                method,
                url,
                headers=dict(headers or {}),
                params=dict(params or {}),
                data=data,
                json=json_body,
            )

    # ------------------------------------------------------------------ secrets/errors

    def _secret_values(self) -> tuple[str, ...]:
        values = [
            self.lwa_client_secret,
            self.refresh_token,
            self._access_token or "",
            self.lwa_client_id,
        ]
        return tuple(value for value in values if value)

    def _scrub(self, text: str) -> str:
        """Strip any credential material that a remote body might echo back at us."""
        scrubbed = text
        for secret in self._secret_values():
            scrubbed = scrubbed.replace(secret, "***")
        return scrubbed

    @staticmethod
    def _remote_summary(response: httpx.Response) -> str:
        """Whitelisted error summary: SP-API ``errors[].code/message`` only."""
        try:
            payload = response.json()
        except ValueError:
            return "No readable error payload."
        if not isinstance(payload, Mapping):
            return "No readable error payload."
        parts: list[str] = []
        for issue in payload.get("errors") or []:
            if not isinstance(issue, Mapping):
                continue
            code = str(issue.get("code") or "").strip()
            message = str(issue.get("message") or "").strip()
            text = f"{code}: {message}" if code and message else (message or code)
            if text:
                parts.append(text[:200])
        return " | ".join(parts)[:500] or "No readable error payload."

    def _auth_error(self, detail: str) -> AppError:
        return AppError(
            slug="amazon-auth-failed",
            status_code=502,
            code="ERR_AMAZON_AUTH_FAILED",
            title="Amazon Authentication Failed",
            detail=self._scrub(detail),
        )

    def _rate_error(self, response: httpx.Response) -> AppError:
        retry_after = response.headers.get("Retry-After", "").strip()
        suffix = f" Retry after {retry_after}s." if retry_after else ""
        return AppError(
            slug="amazon-rate-limited",
            status_code=429,
            code="ERR_AMAZON_RATE_LIMITED",
            title="Amazon Rate Limited",
            detail=self._scrub(f"SP-API throttled this request (HTTP 429).{suffix}"),
        )

    def _api_error(self, response: httpx.Response, detail: str | None = None) -> AppError:
        body = detail or self._remote_summary(response)
        return AppError(
            slug="amazon-sp-api-error",
            status_code=502,
            code="ERR_AMAZON_SP_API_ERROR",
            title="Amazon SP-API Error",
            detail=self._scrub(f"{body} (HTTP {response.status_code})"),
        )

    def _listing_rejected(self, detail: str) -> AppError:
        return AppError(
            slug="amazon-listing-rejected",
            status_code=502,
            code="ERR_AMAZON_LISTING_REJECTED",
            title="Amazon Listing Rejected",
            detail=self._scrub(detail),
        )

    # ------------------------------------------------------------------ LWA auth

    def _invalidate_token(self) -> None:
        self._access_token = None
        self._token_expires_at = 0.0

    async def _get_access_token(self) -> str:
        """Return a cached LWA access token, refreshing it 60s before real expiry."""
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            # Another task may have refreshed while we waited for the lock.
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at:
                return self._access_token
            response = await self._send(
                "POST",
                LWA_TOKEN_URL,
                headers={
                    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.lwa_client_id,
                    "client_secret": self.lwa_client_secret,
                },
            )
            status = response.status_code
            if status in (400, 401, 403):
                raise self._auth_error(
                    "Login with Amazon refused the refresh-token exchange "
                    f"(HTTP {status}); re-authorize the seller account."
                )
            if status == 429:
                raise self._rate_error(response)
            if status >= 400:
                raise self._api_error(response, detail="Login with Amazon token exchange failed.")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            token = payload.get("access_token") if isinstance(payload, Mapping) else None
            if not isinstance(token, str) or not token:
                raise self._auth_error(
                    "Login with Amazon returned no access token; refusing to call SP-API."
                )
            try:
                expires_in = int(payload.get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires_in = 3600
            # 60s buffer so an in-flight request never races the real expiry instant.
            self._access_token = token
            self._token_expires_at = time.monotonic() + max(0, expires_in - 60)
            return token

    async def _sp_request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Send an authenticated SP-API request, raising only auth/rate failures."""
        token = await self._get_access_token()
        headers: dict[str, str] = {
            "x-amz-access-token": token,
            "accept": "application/json",
            "user-agent": "omnichannel-erp-amazon-sp/1.0",
        }
        if json_body is not None:
            headers["content-type"] = "application/json"
        response = await self._send(
            method, url, headers=headers, params=params, json_body=json_body
        )
        if response.status_code in (401, 403):
            self._invalidate_token()
            raise self._auth_error(
                "SP-API rejected the access token "
                f"(HTTP {response.status_code}); the cached token was discarded."
            )
        if response.status_code == 429:
            raise self._rate_error(response)
        return response

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        response = await self._sp_request(method, url, params=params, json_body=json_body)
        if response.status_code >= 400:
            raise self._api_error(response)
        return self._json_object(response)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    # ------------------------------------------------------------------ order parsing

    @staticmethod
    def _money(container: Any) -> Decimal:
        """Convert one SP-API ``Money`` object (``{"Amount": "12.34"}``) to Decimal."""
        raw: Any = None
        if isinstance(container, Mapping):
            raw = container.get("Amount")
        if raw in (None, ""):
            return Decimal("0.00")
        try:
            return Decimal(str(raw)).quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError) as exc:
            raise AppError(
                slug="amazon-invalid-amount",
                status_code=502,
                code="ERR_AMAZON_INVALID_AMOUNT",
                title="Amazon Invalid Amount",
                detail=f"SP-API returned a non-numeric amount '{str(raw)[:40]}'.",
            ) from exc

    @staticmethod
    def _int(value: Any, default: int) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default

    def _normalize_line_item(self, raw: Mapping[str, Any]) -> NormalizedLineItem | None:
        sku = str(raw.get("SellerSKU") or raw.get("ASIN") or "").strip()
        if not sku:
            return None
        quantity = self._int(raw.get("QuantityOrdered"), 1)
        if quantity < 1:
            return None
        line_total = self._money(raw.get("ItemPrice"))
        if line_total == Decimal("0"):
            # Some order types omit ItemPrice but still ship UnitPrice.
            unit_price = self._money(raw.get("UnitPrice"))
            line_total = (unit_price * Decimal(quantity)).quantize(
                _MONEY_SCALE, rounding=ROUND_HALF_UP
            )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(raw.get("Title") or sku),
            quantity=quantity,
            line_total=line_total,
        )

    async def _fetch_order_items(self, order_id: str) -> list[Mapping[str, Any]]:
        """Fetch every ``orderItems`` page for one order (bounded, no infinite loop)."""
        items: list[Mapping[str, Any]] = []
        next_token: str | None = None
        for _ in range(_ORDER_ITEM_PAGE_LIMIT):
            params: dict[str, Any] = {"MarketplaceIds": self.marketplace_id}
            if next_token:
                params["NextToken"] = next_token
            page = await self._request_json(
                "GET",
                f"{self.endpoint}/orders/v0/orders/{order_id}/orderItems",
                params=params,
            )
            payload = page.get("payload")
            payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
            for row in payload_map.get("OrderItems") or []:
                if isinstance(row, Mapping):
                    items.append(row)
            token = payload_map.get("NextToken")
            next_token = str(token) if token else None
            if not next_token:
                break
        return items

    @staticmethod
    def _payment_status(status: str, payment_method: str) -> PaymentStatus:
        lowered = status.strip().lower()
        if lowered in _CANCELED_STATUSES or lowered in {"refunded", "returned", "fully_refunded"}:
            # A cancelled/refunded Amazon order is settled back to the buyer: the ERP receivable
            # must be refunded, never left open.
            return PaymentStatus.REFUNDED
        if lowered in _UNPAID_STATUSES and not payment_method:
            return PaymentStatus.UNPAID
        return PaymentStatus.PAID

    async def _normalize_order(self, raw: Mapping[str, Any]) -> NormalizedOrder | None:
        order_id = str(raw.get("AmazonOrderId") or "").strip()
        if not order_id:
            return None
        raw_items = await self._fetch_order_items(order_id)
        line_items = [
            line for item in raw_items if (line := self._normalize_line_item(item)) is not None
        ]
        if not line_items:
            # NormalizedOrder requires at least one line; skipping is honest, importing a
            # zero-line order would break the reservation path downstream.
            logger.warning("amazon_order_without_line_items", external_order_id=order_id)
            return None

        status = str(raw.get("OrderStatus") or "").strip()
        payment_method = str(raw.get("PaymentMethod") or "").strip()
        order_total = raw.get("OrderTotal")
        currency = (
            str(order_total.get("CurrencyCode") or "USD").strip().upper()
            if isinstance(order_total, Mapping)
            else "USD"
        )
        subtotal = sum((item.line_total for item in line_items), start=Decimal("0.00")).quantize(
            _MONEY_SCALE, rounding=ROUND_HALF_UP
        )
        shipping = sum(
            (self._money(item.get("ShippingPrice")) for item in raw_items),
            start=Decimal("0.00"),
        ).quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
        tax = sum(
            (self._money(item.get("ItemTax")) for item in raw_items),
            start=Decimal("0.00"),
        ).quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
        promotion = sum(
            (self._money(item.get("PromotionDiscount")) for item in raw_items),
            start=Decimal("0.00"),
        ).quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
        # Amazon reports promotion discounts as negatives; the ERP stores a magnitude.
        discount = abs(promotion)
        total = self._money(order_total)
        if total == Decimal("0"):
            total = (subtotal + shipping + tax - discount).quantize(
                _MONEY_SCALE, rounding=ROUND_HALF_UP
            )

        buyer = raw.get("BuyerInfo")
        buyer_map: Mapping[str, Any] = buyer if isinstance(buyer, Mapping) else {}
        address = raw.get("ShippingAddress")
        address_map: Mapping[str, Any] = address if isinstance(address, Mapping) else {}
        buyer_email = str(buyer_map.get("BuyerEmail") or "").strip() or None
        buyer_phone = str(address_map.get("Phone") or "").strip() or None

        metadata: dict[str, Any] = {
            "amazon_order_status": status,
            "amazon_marketplace_id": str(raw.get("MarketplaceId") or self.marketplace_id),
            "amazon_fulfillment_channel": str(raw.get("FulfillmentChannel") or ""),
            "amazon_sales_channel": str(raw.get("SalesChannel") or ""),
            "amazon_is_business_order": bool(raw.get("IsBusinessOrder")),
            "amazon_seller_order_id": str(raw.get("SellerOrderId") or ""),
        }
        refunded_val = self._money(raw.get("RefundedAmount")).copy_abs()
        commission_val = self._money(raw.get("PlatformCommission")) + sum(
            (self._money(item.get("CommissionFee")) for item in raw_items),
            start=Decimal("0.00"),
        )
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded_val,
            platform_commission_base=commission_val,
        )
        status_lower = status.lower()
        is_cancelled = status_lower in _CANCELED_STATUSES or bool(
            raw.get("CancelledAt") or raw.get("cancelled_at")
        )
        is_full_refund = status_lower in {"refunded", "returned", "fully_refunded"} or (
            total > Decimal("0") and refunded_val >= total
        )
        if is_cancelled:
            fulfillment_status = FulfillmentStatus.CANCELLED
            payment_status = PaymentStatus.REFUNDED
        elif is_full_refund:
            fulfillment_status = FulfillmentStatus.REFUNDED
            payment_status = PaymentStatus.REFUNDED
        else:
            fulfillment_status = _ORDER_STATUS_MAP_LOWER.get(
                status_lower, FulfillmentStatus.CONFIRMED_RESERVED
            )
            payment_status = self._payment_status(status, payment_method)
        return NormalizedOrder(
            external_order_id=order_id,
            external_order_number=str(raw.get("SellerOrderId") or order_id),
            order_mode=(
                OrderMode.B2B_WHOLESALE if raw.get("IsBusinessOrder") else OrderMode.B2C_RETAIL
            ),
            currency=currency,
            # Never guess: EcommerceOrderSyncService.resolve_order_fx_rate locks it.
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission_val.quantize(
                _MONEY_SCALE, rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=buyer_email,
                phone_e164=buyer_phone,
                external_customer_id=buyer_email,
                external_identity_type=IdentityType.AMAZON_BUYER_EMAIL,
                display_name=buyer_email or "Amazon Buyer",
                country_code=(str(address_map.get("CountryCode") or "").strip().upper() or None),
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_parse_iso8601(raw.get("LastUpdateDate"))
            or _parse_iso8601(raw.get("OrderDate"))
            or datetime.now(UTC),
        )

    # ------------------------------------------------------------------ orders API

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        """Page ``GET /orders/v0/orders`` and hydrate each order's real line items."""
        params: dict[str, Any] = {"MarketplaceIds": self.marketplace_id}
        if cursor:
            # SP-API forbids LastUpdatedAfter together with NextToken.
            params["NextToken"] = cursor
        else:
            params["LastUpdatedAfter"] = _iso8601_z(since)
            params["MaxResultsPerPage"] = 100
        page = await self._request_json("GET", f"{self.endpoint}/orders/v0/orders", params=params)
        payload = page.get("payload")
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        orders: list[NormalizedOrder] = []
        for raw in payload_map.get("Orders") or []:
            if not isinstance(raw, Mapping):
                continue
            normalized = await self._normalize_order(raw)
            if normalized is not None:
                orders.append(normalized)
        token = payload_map.get("NextToken")
        return orders, (str(token) if token else None)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        """Page ``GET /listings/2021-08-01/items/{sellerId}`` for the seller's listings.

        SP-API Listings Items has no server-side updated-since filter, so items whose
        ``summaries[].lastUpdatedDate`` predates ``since`` are filtered client-side and
        every other listing is emitted. The cursor is the Listings ``pagination.nextToken``.
        """
        params: dict[str, Any] = {
            "marketplaceIds": self.marketplace_id,
            "pageSize": 50,
            "includedData": "summaries",
        }
        if cursor:
            params["pageToken"] = cursor
        page = await self._request_json(
            "GET",
            f"{self.endpoint}/listings/2021-08-01/items/{self.seller_id}",
            params=params,
        )
        since_utc = _as_utc(since)
        products: list[NormalizedProduct] = []
        for row in page.get("items") or []:
            if not isinstance(row, Mapping):
                continue
            sku = str(row.get("sku") or "").strip()
            summaries = [
                summary for summary in row.get("summaries") or [] if isinstance(summary, Mapping)
            ]
            summary = summaries[0] if summaries else {}
            asin = str(summary.get("asin") or "").strip()
            external_id = asin or sku
            if not external_id:
                continue
            last_updated = _parse_iso8601(summary.get("lastUpdatedDate"))
            if last_updated is not None and last_updated < since_utc:
                continue
            raw_status = summary.get("status")
            status_text = (
                ",".join(str(state) for state in raw_status)
                if isinstance(raw_status, Sequence) and not isinstance(raw_status, str)
                else str(raw_status or "")
            )
            products.append(
                NormalizedProduct(
                    external_product_id=external_id,
                    title=str(summary.get("itemName") or sku or external_id),
                    variants=[
                        {
                            "sku": sku,
                            "asin": asin or None,
                            "marketplace_id": self.marketplace_id,
                            "status": status_text,
                            "last_updated_date": summary.get("lastUpdatedDate"),
                        }
                    ],
                )
            )
        pagination = page.get("pagination")
        pagination_map: Mapping[str, Any] = pagination if isinstance(pagination, Mapping) else {}
        token = pagination_map.get("nextToken")
        return products, (str(token) if token else None)

    # ------------------------------------------------------------------ fulfillment API

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Confirm one shipment through ``POST /orders/v0/orders/{id}/shipmentConfirmation``."""
        ship_date = _iso8601_z(datetime.now(UTC))
        package_ref = (fulfillment_order_id or "").strip() or "1"
        order_items: list[dict[str, Any]] = []
        try:
            raw_items = await self._fetch_order_items(external_order_id)
        except AppError as exc:
            if exc.code != "ERR_AMAZON_SP_API_ERROR":
                raise
            raw_items = []
        for item in raw_items:
            item_id = str(item.get("OrderItemId") or "").strip()
            if not item_id:
                continue
            qty = max(1, self._int(item.get("QuantityOrdered"), 1))
            order_items.append({"orderItemId": item_id, "quantity": qty})
        payload: dict[str, Any] = {
            "marketplaceId": self.marketplace_id,
            "packageDetail": {
                "packageReferenceId": package_ref,
                "carrierCode": carrier_company,
                "trackingNumber": tracking_number,
                "shipDate": ship_date,
                "orderItems": order_items,
            },
        }
        response = await self._sp_request(
            "POST",
            f"{self.endpoint}/orders/v0/orders/{external_order_id}/shipmentConfirmation",
            json_body=payload,
        )
        if response.status_code >= 400:
            raise self._api_error(response, detail="Amazon rejected the shipment confirmation.")
        fulfillment_id = f"{external_order_id}:{package_ref}"
        return {
            "fulfillment_id": fulfillment_id,
            "external_fulfillment_id": fulfillment_id,
            "status": "ACCEPTED",
            "external_order_id": external_order_id,
            "tracking_number": tracking_number,
            "carrier_code": carrier_company,
            "tracking_url": tracking_url,
            "fulfillment_order_id": fulfillment_order_id,
            "ship_date": ship_date,
            "notify_customer": notify_customer,
            "raw_response": self._json_object(response),
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Constant-time HMAC-SHA256 check of an Amazon/SNS notification (fail-closed)."""
        signature = _header_value(headers, "X-Amz-Sns-Signature") or _header_value(
            headers, "X-Amazon-Signature"
        )
        if not signature or not secret:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    # ------------------------------------------------------------------ listings API

    def _listing_item_url(self, sku: str) -> str:
        return f"{self.endpoint}/listings/2021-08-01/items/{self.seller_id}/{sku}"

    def _listing_params(self) -> dict[str, str]:
        return {"marketplaceIds": self.marketplace_id}

    def _build_listing_attributes(
        self,
        *,
        title: str,
        description: str,
        bullet_points: Sequence[str],
        price: Decimal,
        quantity: int,
        images: Sequence[str],
        currency: str,
        category: str | None,
        hs_code: str | None,
        moq_tiers: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        def text_entry(value: str) -> dict[str, str]:
            return {
                "value": value,
                "language_tag": "en_US",
                "marketplace_id": self.marketplace_id,
            }

        attributes: dict[str, Any] = {
            "item_name": [text_entry(title)],
            "product_description": [text_entry(description)],
            "bullet_point": [text_entry(point) for point in bullet_points if str(point).strip()],
            "purchasable_offer": [
                {
                    "marketplace_id": self.marketplace_id,
                    "currency": currency.upper(),
                    "our_price": [
                        {"schedule": [{"value_with_tax": _money_str(price)}]},
                    ],
                }
            ],
            "fulfillment_availability": [
                {
                    "fulfillment_channel_code": "DEFAULT",
                    "quantity": max(0, int(quantity)),
                }
            ],
        }
        listing_images = [str(image).strip() for image in images if str(image).strip()]
        if listing_images:
            attributes["main_product_image_locator"] = [{"media_location": listing_images[0]}]
            for index, image in enumerate(listing_images[1:], start=1):
                attributes[f"other_product_image_locator_{index}"] = [{"media_location": image}]
        if category:
            attributes["item_type_keyword"] = [{"value": category}]
        if hs_code:
            attributes["hs_code"] = [{"value": hs_code}]
        levels: list[dict[str, Any]] = []
        for tier in moq_tiers:
            minimum = tier.get("min_qty")
            price_value = tier.get("unit_price_usd")
            if minimum is None or price_value is None:
                continue
            levels.append(
                {
                    "lower_bound": max(1, self._int(minimum, 1)),
                    "discount": {"value_with_tax": _money_str(Decimal(str(price_value)))},
                }
            )
        if levels:
            attributes["quantity_discount_plan"] = [
                {
                    "marketplace_id": self.marketplace_id,
                    "currency": currency.upper(),
                    "discount_type": "fixed",
                    "levels": levels,
                }
            ]
        return attributes

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
        product_type: str = "PRODUCT",
        category: str | None = None,
        hs_code: str | None = None,
        moq_tiers: Sequence[Mapping[str, Any]] = (),
        options: Sequence[Mapping[str, Any]] | None = None,
        variants: Sequence[Mapping[str, Any]] | None = None,
        image_blobs: Sequence[Mapping[str, Any]] | None = None,
    ) -> AmazonListingReceipt:
        """Create/update one listing through Listings Items v2021-08-01 (``PUT``)."""
        del options, variants, image_blobs
        payload: dict[str, Any] = {
            "productType": product_type,
            "requirements": "LISTING",
            "attributes": self._build_listing_attributes(
                title=title,
                description=description,
                bullet_points=bullet_points,
                price=price_usd,
                quantity=quantity,
                images=images,
                currency=currency,
                category=category,
                hs_code=hs_code,
                moq_tiers=moq_tiers,
            ),
        }
        response = await self._sp_request(
            "PUT",
            self._listing_item_url(sku),
            params=self._listing_params(),
            json_body=payload,
        )
        body = self._json_object(response)
        status = str(body.get("status") or ("ACCEPTED" if response.status_code < 400 else ""))
        issues = body.get("issues")
        issue_list = [dict(issue) for issue in issues or [] if isinstance(issue, Mapping)]
        if response.status_code >= 400 or status.upper() == "INVALID":
            raise self._listing_rejected(
                "Amazon rejected listing "
                f"'{sku}' (status {status or 'HTTP_' + str(response.status_code)}): "
                f"{self._issue_summary(issue_list)}"
            )
        external_product_id = str(body.get("asin") or body.get("submissionId") or sku)
        return AmazonListingReceipt(
            external_product_id=external_product_id,
            external_sku=sku,
            submission_id=(
                str(body["submissionId"]) if body.get("submissionId") is not None else None
            ),
            status=status or "ACCEPTED",
            issues=issue_list,
            raw_response=body,
        )

    def _issue_summary(self, issues: Sequence[Mapping[str, Any]]) -> str:
        parts: list[str] = []
        for issue in issues:
            code = str(issue.get("code") or "").strip()
            message = str(issue.get("message") or "").strip()
            text = f"{code}: {message}" if code and message else (message or code)
            if text:
                parts.append(text[:200])
        return " | ".join(parts)[:500] or "no issue details returned."

    async def push_inventory_level(
        self, *, external_sku: str, quantity: int, **_extra: Any
    ) -> dict[str, Any]:
        """Patch ``fulfillment_availability`` for one SKU through Listings Items ``PATCH``."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="amazon-inventory-sync-failed",
                status_code=422,
                code="ERR_AMAZON_INVENTORY_SYNC",
                title="Amazon Inventory Sync Failed",
                detail="Amazon SP-API inventory sync requires a non-empty external_sku.",
            )
        normalized_quantity = max(0, int(quantity))
        patch: list[dict[str, Any]] = [
            {
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [
                    {
                        "fulfillment_channel_code": "DEFAULT",
                        "quantity": normalized_quantity,
                    }
                ],
            }
        ]
        response = await self._sp_request(
            "PATCH",
            self._listing_item_url(clean_sku),
            params=self._listing_params(),
            json_body=patch,
        )
        body = self._json_object(response)
        status = str(body.get("status") or ("ACCEPTED" if response.status_code < 400 else ""))
        issues = body.get("issues")
        issue_list = [dict(issue) for issue in issues or [] if isinstance(issue, Mapping)]
        if response.status_code >= 400 or status.upper() == "INVALID":
            raise self._listing_rejected(
                "Amazon rejected the inventory update for "
                f"'{clean_sku}' (status {status or 'HTTP_' + str(response.status_code)}): "
                f"{self._issue_summary(issue_list)}"
            )
        return {
            "external_sku": clean_sku,
            "quantity": normalized_quantity,
            "status": status or "ACCEPTED",
            "submission_id": (
                str(body["submissionId"]) if body.get("submissionId") is not None else None
            ),
            "issues": issue_list,
            "raw_response": body,
        }
