"""eBay Sell API connector: OAuth2 refresh-token exchange, order sync, listing publish.

Everything here is deterministic and offline-testable: the adapter owns an
``httpx.MockTransport``-compatible ``transport`` seam, every monetary amount is parsed into
``Decimal`` (never ``float``) and quantized with ``ROUND_HALF_UP``, and no credential
material (``cert_id`` / ``refresh_token`` / ``access_token``) can reach a log record or an
``AppError`` detail.
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

EBAY_PRODUCTION_BASE_URL = "https://api.ebay.com"
EBAY_SANDBOX_BASE_URL = "https://api.sandbox.ebay.com"
EBAY_TOKEN_PATH = "/identity/v1/oauth2/token"
EBAY_ORDERS_PATH = "/sell/fulfillment/v1/order"
EBAY_INVENTORY_ITEM_PATH = "/sell/inventory/v1/inventory_item"
EBAY_OFFER_PATH = "/sell/inventory/v1/offer"
EBAY_BULK_PRICE_QUANTITY_PATH = "/sell/inventory/v1/bulk_update_price_quantity"

#: eBay caps every Sell API page at 50 rows for orders and inventory items.
EBAY_PAGE_LIMIT = 50
#: Refresh access tokens a minute before eBay's own expiry so a live call never races it.
EBAY_TOKEN_EXPIRY_BUFFER_SECONDS = 60.0
#: fallback TTL when eBay omits ``expires_in`` (its documented default is 7200s).
EBAY_DEFAULT_TOKEN_TTL_SECONDS = 7200
#: eBay truncates (and rejects above) 80 chars for a listing title.
EBAY_MAX_TITLE_LENGTH = 80
EBAY_DEFAULT_CATEGORY_ID = "20706"

EBAY_OAUTH_SCOPE = " ".join(
    (
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    )
)

MONEY_SCALE = Decimal("0.01")

#: eBay orderFulfillmentStatus -> internal fulfillment status. ``NOT_STARTED``/``IN_PROGRESS``
#: mean "paid, awaiting our shipment"; ``FULFILLED`` means the seller already shipped.
_FULFILLMENT_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "NOT_STARTED": FulfillmentStatus.CONFIRMED_RESERVED,
    "IN_PROGRESS": FulfillmentStatus.CONFIRMED_RESERVED,
    "FULFILLED": FulfillmentStatus.SHIPPED,
}


class EbayListingReceipt(BaseModel):
    """Published eBay offer: the real listing id is what buyers and the ERP both key on."""

    external_product_id: str
    external_sku: str
    offer_id: str
    listing_id: str
    status: str = "PUBLISHED"
    raw_response: dict[str, Any] = Field(default_factory=dict)


class EbayConnectorAdapter(CommercePlatformAdapter):
    """eBay Sell API adapter (OAuth2 refresh-token grant + Inventory API publish).

    Credentials are held privately and are never part of ``repr``/``str``, log records or
    error details; only ``seller_username``/``site_id``/``base_url`` are safe to echo.
    """

    def __init__(
        self,
        *,
        seller_username: str,
        site_id: str = "EBAY_US",
        app_id: str,
        cert_id: str,
        refresh_token: str,
        sandbox: bool = False,
        api_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.seller_username = seller_username
        self.site_id = site_id
        self.sandbox = sandbox
        self.base_url = (
            api_base_url
            if api_base_url is not None
            else (EBAY_SANDBOX_BASE_URL if sandbox else EBAY_PRODUCTION_BASE_URL)
        ).rstrip("/")
        self._app_id = app_id
        self._cert_id = cert_id
        self._refresh_token = refresh_token
        self._transport = transport
        self._timeout = timeout
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"EbayConnectorAdapter(seller_username={self.seller_username!r}, "
            f"site_id={self.site_id!r}, sandbox={self.sandbox!r}, base_url={self.base_url!r})"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout, trust_env=False)

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a log line or an ``AppError`` detail."""
        return tuple(
            value
            for value in (self._cert_id, self._refresh_token, self._access_token or "")
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
        """Map the credential/rate outcomes eBay expects callers to handle explicitly."""
        if response.status_code in (401, 403):
            raise AppError(
                slug="ebay-auth-failed",
                status_code=502,
                code="ERR_EBAY_AUTH_FAILED",
                title="eBay Authentication Failed",
                detail=(
                    "eBay rejected the OAuth2 credentials for the Sell API "
                    f"(HTTP {response.status_code})."
                ),
            )
        if response.status_code == 429:
            raise AppError(
                slug="ebay-rate-limited",
                status_code=429,
                code="ERR_EBAY_RATE_LIMITED",
                title="eBay Rate Limited",
                detail=(
                    "eBay returned HTTP 429 for the Sell API call; retry after the "
                    "advertised backoff."
                ),
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._access_token_or_refresh()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        async with self._client() as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=dict(params) if params is not None else None,
                    json=dict(json_body) if json_body is not None else None,
                    headers=request_headers,
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="ebay-unreachable",
                    status_code=502,
                    code="ERR_EBAY_UNREACHABLE",
                    title="eBay Unreachable",
                    detail=(
                        f"eBay Sell API call '{method} {path}' failed before any response arrived."
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
        """Return a cached access token, refreshing only once the 60s buffer is exhausted."""
        if self._access_token is not None and time.monotonic() < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token is not None and time.monotonic() < self._token_expires_at:
                return self._access_token
            return await self._refresh_access_token()

    async def _refresh_access_token(self) -> str:
        basic = base64.b64encode(f"{self._app_id}:{self._cert_id}".encode()).decode()
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}{EBAY_TOKEN_PATH}",
                    headers={
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "scope": EBAY_OAUTH_SCOPE,
                    },
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="ebay-auth-failed",
                    status_code=502,
                    code="ERR_EBAY_AUTH_FAILED",
                    title="eBay Authentication Failed",
                    detail="eBay's OAuth2 token endpoint was unreachable.",
                ) from None
        self._raise_for_transport_status(response)
        if response.status_code >= 400:
            raise AppError(
                slug="ebay-auth-failed",
                status_code=502,
                code="ERR_EBAY_AUTH_FAILED",
                title="eBay Authentication Failed",
                detail=(
                    "eBay refused the refresh-token exchange for the Sell API "
                    f"(HTTP {response.status_code})."
                ),
            )
        payload = self._json_object(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise AppError(
                slug="ebay-auth-failed",
                status_code=502,
                code="ERR_EBAY_AUTH_FAILED",
                title="eBay Authentication Failed",
                detail="eBay returned an OAuth2 token response without a usable access_token.",
            )
        ttl = self._as_int(payload.get("expires_in"), EBAY_DEFAULT_TOKEN_TTL_SECONDS)
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(
            float(ttl) - EBAY_TOKEN_EXPIRY_BUFFER_SECONDS, 0.0
        )
        logger.info(
            "ebay_access_token_refreshed",
            seller_username=self.seller_username,
            site_id=self.site_id,
            sandbox=self.sandbox,
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
        """Parse an eBay money value into 2dp ``Decimal``; never fall back to ``float``."""
        if value is None or value == "":
            return Decimal("0.00").quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        try:
            return Decimal(str(value)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        except (ArithmeticError, ValueError):
            raise AppError(
                slug="ebay-invalid-money",
                status_code=502,
                code="ERR_EBAY_INVALID_MONEY",
                title="Invalid eBay Money Value",
                detail="eBay returned a monetary value that is not a finite decimal number.",
            ) from None

    @classmethod
    def _money(cls, payload: Mapping[str, Any] | None, key: str) -> Decimal:
        """Read ``{"key": {"value": "12.34", "currency": "USD"}}`` (or a bare value)."""
        if not isinstance(payload, Mapping):
            return cls._as_decimal(None)
        amount = payload.get(key)
        if isinstance(amount, Mapping):
            return cls._as_decimal(amount.get("value"))
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
        """eBay's filter/payload timestamp form: ``2024-05-01T00:00:00.000Z`` (UTC)."""
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
                slug="ebay-invalid-cursor",
                status_code=422,
                code="ERR_EBAY_INVALID_CURSOR",
                title="Invalid eBay Cursor",
                detail="eBay offset cursors are non-negative integers.",
            ) from None

    @staticmethod
    def _next_cursor(*, offset: int, fetched: int, total: Any) -> str | None:
        """Advance the offset cursor while eBay reports more rows than we just read."""
        if fetched <= 0:
            return None
        advanced = offset + fetched
        total_count = EbayConnectorAdapter._as_int(total, -1)
        if total_count < 0:
            # eBay omitted ``total``: a short page is the only honest end-of-feed signal.
            return str(advanced) if fetched >= EBAY_PAGE_LIMIT else None
        return str(advanced) if total_count > advanced else None

    # -------------------------------------------------------------------- normalization

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        sku = str(item.get("sku") or item.get("legacyItemId") or "").strip()
        if not sku:
            return None
        quantity = max(self._as_int(item.get("quantity"), 1), 1)
        # eBay's ``lineItemCost`` is the line total; ``total`` is the older alias.
        cost = item.get("lineItemCost")
        fallback = item.get("total")
        if isinstance(cost, Mapping):
            line_total = self._as_decimal(cost.get("value"))
        elif isinstance(fallback, Mapping):
            line_total = self._as_decimal(fallback.get("value"))
        else:
            line_total = self._as_decimal(fallback)
        return NormalizedLineItem(
            external_sku=sku,
            title=str(item.get("title") or sku),
            quantity=quantity,
            line_total=line_total,
        )

    def _normalize_order(self, order: Mapping[str, Any]) -> NormalizedOrder | None:
        external_order_id = str(order.get("orderId") or "").strip()
        if not external_order_id:
            return None
        line_items = [
            line
            for item in order.get("lineItems") or []
            if isinstance(item, Mapping) and (line := self._normalize_line_item(item)) is not None
        ]
        if not line_items:
            return None

        pricing = order.get("pricingSummary")
        pricing_summary = pricing if isinstance(pricing, Mapping) else {}
        subtotal = self._money(pricing_summary, "priceSubtotal")
        shipping = self._money(pricing_summary, "deliveryCost")
        tax = self._money(pricing_summary, "tax")
        total = (
            self._money(pricing_summary, "total")
            if pricing_summary.get("total") is not None
            else (subtotal + shipping + tax).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        )

        cancel = order.get("cancelStatus")
        cancel_state = (
            str((cancel.get("cancelState") if isinstance(cancel, Mapping) else "") or "")
            .strip()
            .upper()
        )
        cancelled_at = order.get("cancelledAt") or order.get("canceledAt")
        is_cancelled = cancel_state in {"CANCELED", "CANCELLED"} or bool(cancelled_at)

        payment_summary = order.get("paymentSummary")
        payment_map = payment_summary if isinstance(payment_summary, Mapping) else {}
        refund_rows = payment_map.get("refunds") or []
        refunded_from_rows = sum(
            (
                self._money(r, "amount").copy_abs()
                for r in refund_rows
                if isinstance(r, Mapping) and isinstance(r.get("amount"), Mapping)
            ),
            start=Decimal("0.00"),
        )
        refunded_val = (
            refunded_from_rows
            if refunded_from_rows > Decimal("0")
            else self._as_decimal(order.get("refunded_amount")).copy_abs()
        )
        declared_payment = str(order.get("orderPaymentStatus") or "").strip().upper()
        is_full_refund = declared_payment in {"FULLY_REFUNDED", "REFUNDED"} or (
            total > Decimal("0") and refunded_val >= total
        )

        if is_cancelled:
            fulfillment_status = FulfillmentStatus.CANCELLED
        elif is_full_refund:
            fulfillment_status = FulfillmentStatus.REFUNDED
        elif declared_payment in {"FAILED", "PENDING", "UNPAID"}:
            fulfillment_status = FulfillmentStatus.PENDING_PAYMENT
        else:
            fulfillment_status = _FULFILLMENT_STATUS_MAP.get(
                str(order.get("orderFulfillmentStatus") or "").strip().upper(),
                FulfillmentStatus.CONFIRMED_RESERVED,
            )
        payment_status = self._payment_status(
            order,
            canceled=is_cancelled,
            full_refund=is_full_refund,
            refunded_val=refunded_val,
        )

        buyer = order.get("buyer")
        buyer_payload = buyer if isinstance(buyer, Mapping) else {}
        address = buyer_payload.get("buyerRegistrationAddress")
        address_payload = address if isinstance(address, Mapping) else {}
        username = str(buyer_payload.get("username") or "").strip()
        email = str(address_payload.get("email") or "").strip() or None
        phone = address_payload.get("primaryPhone") or address_payload.get("phoneNumber")
        phone_e164 = None
        if isinstance(phone, Mapping):
            phone_e164 = str(phone.get("phoneNumber") or "").strip() or None
        elif phone not in (None, ""):
            phone_e164 = str(phone).strip() or None
        country = str(address_payload.get("countryCode") or "").strip() or None

        commission_val = (
            self._money(order, "totalMarketplaceFee")
            if isinstance(order.get("totalMarketplaceFee"), Mapping)
            else self._money(pricing_summary, "fee")
        )
        ebay_metadata: dict[str, Any] = {
            "ebay_order_id": external_order_id,
            "ebay_seller_id": order.get("sellerId"),
            "ebay_site_id": self.site_id,
            "ebay_fulfillment_status": order.get("orderFulfillmentStatus"),
            "ebay_cancel_state": cancel_state or None,
            "ebay_order_payment_status": order.get("orderPaymentStatus"),
        }
        if cancelled_at:
            ebay_metadata["cancelled_at"] = str(cancelled_at)
        attach_financial_metadata(
            ebay_metadata,
            refunded_amount=refunded_val,
            platform_commission_base=commission_val,
        )
        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=external_order_id,
            order_mode=OrderMode.B2C_RETAIL,
            currency=str(pricing_summary.get("currency") or "USD").strip().upper() or "USD",
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission_val.quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone_e164,
                external_customer_id=username or None,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=username or email or "eBay Buyer",
                country_code=country,
            ),
            line_items=line_items,
            channel_metadata=ebay_metadata,
            updated_at=self._parse_datetime(
                order.get("lastModifiedDate") or order.get("creationDate")
            ),
        )

    @staticmethod
    def _payment_status(
        order: Mapping[str, Any],
        *,
        canceled: bool,
        full_refund: bool = False,
        refunded_val: Decimal = Decimal("0.00"),
    ) -> PaymentStatus:
        declared = str(order.get("orderPaymentStatus") or "").strip().upper()
        if canceled:
            if declared in {"FAILED", "PENDING", "UNPAID"} and refunded_val == Decimal("0"):
                return PaymentStatus.UNPAID
            return PaymentStatus.REFUNDED
        if full_refund or declared in {"FULLY_REFUNDED", "REFUNDED"}:
            return PaymentStatus.REFUNDED
        if declared in {"FAILED", "PENDING", "UNPAID"}:
            return PaymentStatus.UNPAID
        if declared == "PARTIALLY_PAID":
            return PaymentStatus.PARTIALLY_PAID
        return PaymentStatus.PAID

    # ---------------------------------------------------------------- CommercePlatformAdapter

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        offset = self._parse_offset(cursor)
        response = await self._request(
            "GET",
            EBAY_ORDERS_PATH,
            params={
                "filter": f"lastmodifieddate:[{self._format_datetime(since)}..]",
                "limit": EBAY_PAGE_LIMIT,
                "offset": offset,
            },
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="ebay-order-sync-failed",
                code="ERR_EBAY_ORDER_SYNC_FAILED",
                title="eBay Order Sync Failed",
            )
        payload = self._json_object(response)
        raw_orders = payload.get("orders")
        rows = raw_orders if isinstance(raw_orders, list) else []
        orders = [
            order
            for row in rows
            if isinstance(row, Mapping) and (order := self._normalize_order(row)) is not None
        ]
        # eBay's ``offset`` counts raw order rows, so the cursor must advance by what the
        # feed actually returned — even when a digital/no-line-item row is skipped.
        return orders, self._next_cursor(
            offset=offset, fetched=len(rows), total=payload.get("total")
        )

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        # The Inventory API exposes no modified-since filter: ``since`` is accepted for the
        # shared connector contract and the feed is walked with limit/offset instead.
        offset = self._parse_offset(cursor)
        response = await self._request(
            "GET",
            EBAY_INVENTORY_ITEM_PATH,
            params={"limit": EBAY_PAGE_LIMIT, "offset": offset},
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="ebay-product-sync-failed",
                code="ERR_EBAY_PRODUCT_SYNC_FAILED",
                title="eBay Product Sync Failed",
            )
        payload = self._json_object(response)
        raw_items = payload.get("inventoryItems")
        rows = raw_items if isinstance(raw_items, list) else []
        products: list[NormalizedProduct] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            product = self._normalize_product(row)
            if product is not None:
                products.append(product)
        return products, self._next_cursor(
            offset=offset, fetched=len(rows), total=payload.get("total")
        )

    def _normalize_product(self, row: Mapping[str, Any]) -> NormalizedProduct | None:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            return None
        product = row.get("product")
        product_payload = product if isinstance(product, Mapping) else {}
        title = str(product_payload.get("title") or sku)
        availability = row.get("availability")
        availability_payload = availability if isinstance(availability, Mapping) else {}
        ship_to_location = availability_payload.get("shipToLocationAvailability")
        ship_to_payload = ship_to_location if isinstance(ship_to_location, Mapping) else {}
        images = product_payload.get("imageUrls")
        return NormalizedProduct(
            external_product_id=sku,
            title=title,
            variants=[
                {
                    "sku": sku,
                    "title": title,
                    "quantity": max(self._as_int(ship_to_payload.get("quantity"), 0), 0),
                    "condition": row.get("condition"),
                    "images": list(images) if isinstance(images, list) else [],
                    "gtin": product_payload.get("ean") or product_payload.get("upc"),
                }
            ],
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
        """Create the shipping fulfillment eBay shows to the buyer (one call per order)."""
        del tracking_url, fulfillment_order_id, notify_customer
        if not tracking_number.strip():
            raise AppError(
                slug="ebay-tracking-number-required",
                status_code=422,
                code="ERR_EBAY_TRACKING_NUMBER_REQUIRED",
                title="eBay Tracking Number Required",
                detail="eBay refuses a shipping fulfillment without a tracking number.",
            )
        encoded_order_id = quote(external_order_id.strip(), safe="")
        line_items: list[dict[str, Any]] = []
        order_resp = await self._request("GET", f"{EBAY_ORDERS_PATH}/{encoded_order_id}")
        if order_resp.status_code == 200:
            order_data = self._json_object(order_resp)
            for li in order_data.get("lineItems") or []:
                if not isinstance(li, Mapping):
                    continue
                li_id = str(li.get("lineItemId") or "").strip()
                if not li_id:
                    continue
                line_items.append(
                    {"lineItemId": li_id, "quantity": max(self._as_int(li.get("quantity"), 1), 1)}
                )
        path = f"{EBAY_ORDERS_PATH}/{encoded_order_id}/shipping_fulfillment"
        response = await self._request(
            "POST",
            path,
            json_body={
                "shippingCarrierCode": carrier_company,
                "trackingNumber": tracking_number,
                "shippedDate": self._format_datetime(datetime.now(UTC)),
                "lineItems": line_items,
            },
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="ebay-fulfillment-rejected",
                code="ERR_EBAY_FULFILLMENT_REJECTED",
                title="eBay Fulfillment Rejected",
            )
        data = self._json_object(response)
        fid = str(data.get("fulfillment_id") or data.get("fulfillmentId") or "").strip()
        if not fid:
            location = str(
                response.headers.get("Location") or response.headers.get("location") or ""
            ).strip()
            if location:
                fid = location.rstrip("/").rsplit("/", 1)[-1].strip()
        if fid:
            data["fulfillment_id"] = fid
            data.setdefault("fulfillmentId", fid)
        return data

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Fail-closed constant-time HMAC-SHA256 check of ``X-EBAY-SIGNATURE``."""
        signature = str(headers.get("X-EBAY-SIGNATURE") or headers.get("x-ebay-signature") or "")
        signature = signature.strip()
        if not secret or not signature or not raw_body:
            return False
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
        encoded = base64.b64encode(digest).decode()
        # eBay has shipped both hex and base64 signatures; accept either, fail closed.
        for expected in (digest.hex(), encoded, encoded.rstrip("=")):
            if hmac.compare_digest(expected, signature) or hmac.compare_digest(
                expected, signature.lower()
            ):
                return True
        return False

    # ------------------------------------------------------------ Inventory API publishing

    @staticmethod
    def _compose_listing_description(
        description: str,
        bullet_points: Sequence[str],
        *,
        category: str | None,
        hs_code: str | None,
        moq_tiers: Sequence[Mapping[str, Any]],
    ) -> str:
        blocks = [description.strip()]
        bullets = [str(point).strip() for point in bullet_points if str(point).strip()]
        if bullets:
            blocks.append("\n".join(f"- {point}" for point in bullets))
        details: list[str] = []
        if category:
            details.append(f"- Category: {category}")
        if hs_code:
            details.append(f"- HS code: {hs_code}")
        for tier in moq_tiers:
            minimum = tier.get("min_quantity") or tier.get("minQuantity") or tier.get("moq")
            unit_price = tier.get("unit_price") or tier.get("price")
            if minimum is None or unit_price is None:
                continue
            details.append(f"- MOQ {minimum}: {unit_price} per unit")
        if details:
            blocks.append("\n".join(details))
        return "\n\n".join(block for block in blocks if block)

    @staticmethod
    def _extract_offer_id(payload: Mapping[str, Any]) -> str | None:
        """Reuse the existing offer id eBay echoes when a SKU already has one (HTTP 409)."""
        direct = payload.get("offerId")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return None
        for error in errors:
            if not isinstance(error, Mapping):
                continue
            parameters = error.get("parameters")
            if isinstance(parameters, Mapping):
                value = parameters.get("offerId") or parameters.get("offerid")
                if value not in (None, ""):
                    return str(value).strip()
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if not isinstance(parameter, Mapping):
                    continue
                if str(parameter.get("name") or "").strip().lower() != "offerid":
                    continue
                value = parameter.get("value")
                if value not in (None, ""):
                    return str(value).strip()
        return None

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
        category_id: str = EBAY_DEFAULT_CATEGORY_ID,
        merchant_location_key: str = "default",
        category: str | None = None,
        hs_code: str | None = None,
        moq_tiers: Sequence[Mapping[str, Any]] = (),
        options: Sequence[Mapping[str, Any]] | None = None,
        variants: Sequence[Mapping[str, Any]] | None = None,
        image_blobs: Sequence[Mapping[str, Any]] | None = None,
    ) -> EbayListingReceipt:
        """3-step eBay Inventory publish: inventory item -> offer -> offer publish.

        ``category``/``hs_code``/``moq_tiers`` are merchandising context rendered into the
        listing description; the eBay leaf category is ``category_id``.
        """
        del options, variants, image_blobs
        cleaned_sku = sku.strip()
        if not cleaned_sku:
            raise AppError(
                slug="ebay-listing-rejected",
                status_code=502,
                code="ERR_EBAY_LISTING_REJECTED",
                title="eBay Listing Rejected",
                detail="An eBay listing requires a non-empty seller SKU.",
            )
        available_quantity = max(1, self._as_int(quantity, 1))
        if isinstance(price_usd, float):
            raise AppError(
                slug="ebay-invalid-money",
                status_code=422,
                code="ERR_EBAY_INVALID_MONEY",
                title="Invalid eBay Money Value",
                detail=(
                    "eBay listing prices must be Decimal; IEEE-754 float prices are refused so "
                    "no binary rounding error can reach a live listing."
                ),
            )
        listing_title = title.strip()[:EBAY_MAX_TITLE_LENGTH]
        listing_description = self._compose_listing_description(
            description,
            bullet_points,
            category=category,
            hs_code=hs_code,
            moq_tiers=moq_tiers,
        )
        price_value = str(Decimal(price_usd).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP))
        language_headers = {"Content-Language": "en-US"}
        sku_path = quote(cleaned_sku, safe="")

        # Step 1 — inventory item (idempotent PUT; 204 on success).
        product: dict[str, Any] = {
            "title": listing_title,
            "description": listing_description,
        }
        image_urls = [str(url).strip() for url in images if str(url).strip()]
        if image_urls:
            product["imageUrls"] = image_urls
        inventory_response = await self._request(
            "PUT",
            f"{EBAY_INVENTORY_ITEM_PATH}/{sku_path}",
            json_body={
                "product": product,
                "condition": "NEW",
                "availability": {"shipToLocationAvailability": {"quantity": available_quantity}},
            },
            headers=language_headers,
        )
        if inventory_response.status_code >= 300:
            self._raise_api_error(
                inventory_response,
                slug="ebay-listing-rejected",
                code="ERR_EBAY_LISTING_REJECTED",
                title="eBay Listing Rejected",
            )

        # Step 2 — offer (409 means the SKU already has one; reuse the id eBay hands back).
        offer_response = await self._request(
            "POST",
            EBAY_OFFER_PATH,
            json_body={
                "sku": cleaned_sku,
                "marketplaceId": self.site_id,
                "format": "FIXED_PRICE",
                "availableQuantity": available_quantity,
                "categoryId": category_id,
                "listingDescription": listing_description,
                "merchantLocationKey": merchant_location_key,
                "pricingSummary": {"price": {"value": price_value, "currency": currency.upper()}},
            },
            headers=language_headers,
        )
        if offer_response.status_code >= 400 and offer_response.status_code != 409:
            self._raise_api_error(
                offer_response,
                slug="ebay-listing-rejected",
                code="ERR_EBAY_LISTING_REJECTED",
                title="eBay Listing Rejected",
            )
        offer_id = self._extract_offer_id(self._json_object(offer_response))
        if offer_id is None:
            self._raise_api_error(
                offer_response,
                slug="ebay-listing-rejected",
                code="ERR_EBAY_LISTING_REJECTED",
                title="eBay Listing Rejected",
            )

        # Step 3 — publish the offer and capture the real listing id.
        publish_response = await self._request(
            "POST",
            f"{EBAY_OFFER_PATH}/{quote(offer_id, safe='')}/publish",
            json_body={},
            headers=language_headers,
        )
        publish_payload = self._json_object(publish_response)
        listing_id = str(publish_payload.get("listingId") or "").strip()
        if publish_response.status_code >= 300 or not listing_id:
            self._raise_api_error(
                publish_response,
                slug="ebay-listing-rejected",
                code="ERR_EBAY_LISTING_REJECTED",
                title="eBay Listing Rejected",
            )
        logger.info(
            "ebay_listing_published",
            seller_username=self.seller_username,
            site_id=self.site_id,
            sku=cleaned_sku,
            offer_id=offer_id,
            listing_id=listing_id,
        )
        return EbayListingReceipt(
            external_product_id=listing_id,
            external_sku=cleaned_sku,
            offer_id=offer_id,
            listing_id=listing_id,
            status="PUBLISHED",
            raw_response=publish_payload,
        )

    async def push_inventory_level(
        self, *, external_sku: str, quantity: int, **_extra: Any
    ) -> dict[str, Any]:
        """Bulk-push one SKU's sellable quantity so eBay never oversells ERP stock."""
        cleaned_sku = external_sku.strip()
        if not cleaned_sku:
            raise AppError(
                slug="ebay-inventory-update-rejected",
                status_code=422,
                code="ERR_EBAY_INVENTORY_UPDATE_REJECTED",
                title="eBay Inventory Update Rejected",
                detail="An eBay inventory update requires a non-empty seller SKU.",
            )
        response = await self._request(
            "POST",
            EBAY_BULK_PRICE_QUANTITY_PATH,
            json_body={
                "requests": [
                    {
                        "sku": cleaned_sku,
                        "shipToLocationAvailability": {
                            "quantity": max(0, self._as_int(quantity, 0))
                        },
                    }
                ]
            },
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="ebay-inventory-update-rejected",
                code="ERR_EBAY_INVENTORY_UPDATE_REJECTED",
                title="eBay Inventory Update Rejected",
            )
        payload = self._json_object(response)
        results = payload.get("responses")
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, Mapping):
                    continue
                if self._as_int(result.get("statusCode"), 200) >= 400:
                    raise AppError(
                        slug="ebay-inventory-update-rejected",
                        status_code=502,
                        code="ERR_EBAY_INVENTORY_UPDATE_REJECTED",
                        title="eBay Inventory Update Rejected",
                        detail=self._redacted(str(result)),
                    )
        return payload
