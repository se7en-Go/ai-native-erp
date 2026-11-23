"""Ozon Seller API connector: posting sync, listing import, stocks, FBS shipping.

Wire facts this module encodes:

* Authentication is the ``Client-Id`` + ``Api-Key`` header pair; both are sent on every
  request and the API key is scrubbed from ``repr``, log lines and ``AppError`` details.
* Money arrives as JSON *strings* (``"1290.00"``). Every amount is parsed straight into
  :class:`decimal.Decimal` and quantized ``ROUND_HALF_UP``; no ``float`` ever touches a
  price, commission or refund, so a posting cannot inherit a binary rounding error.
* The order list endpoints (``/v3/posting/fbs/list`` for seller warehouses,
  ``/v2/posting/fbo/list`` for Ozon warehouses) page by ``offset``; the product list
  pages by opaque ``last_id``. Both cursors are surfaced as the caller's ``cursor``.
* ``/v3/product/import`` is asynchronous: it answers with a ``task_id``. A live listing
  therefore costs three honest calls (import -> import-info -> product/update for the
  merchandising copy) plus, when configured, batching the stock and picture pushes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
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

OZON_API_BASE = "https://api-seller.ozon.ru"

OZON_FBS_ORDER_LIST_PATH = "/v3/posting/fbs/list"
OZON_FBO_ORDER_LIST_PATH = "/v2/posting/fbo/list"
OZON_FBS_POSTING_GET_PATH = "/v2/posting/fbs/get"
OZON_FBS_SHIP_PATH = "/v2/posting/fbs/ship"
OZON_PRODUCT_LIST_PATH = "/v3/product/list"
OZON_PRODUCT_INFO_LIST_PATH = "/v2/product/info/list"
OZON_PRODUCT_IMPORT_PATH = "/v3/product/import"
OZON_PRODUCT_IMPORT_INFO_PATH = "/v1/product/import/info"
OZON_PRODUCT_UPDATE_PATH = "/v1/product/update"
OZON_PICTURE_IMPORT_PATH = "/v1/product/pictures/import"
OZON_STOCK_IMPORT_PATH = "/v1/product/import/stocks"

#: Signed webhook header carrying the hex HMAC-SHA256 digest of the raw body.
OZON_WEBHOOK_SIGNATURE_HEADER = "x-ozon-signature"

#: Ozon warehouses are selected per request; ``fbs`` ships from the seller, ``fbo`` from Ozon.
OZON_FULFILLMENT_MODES: frozenset[str] = frozenset({"fbs", "fbo"})

#: Listing prices are quoted in the seller contract currency; the ERP quotes USD.
OZON_LISTING_CURRENCY = "USD"

OZON_ORDER_PAGE_LIMIT = 1000
OZON_PRODUCT_PAGE_LIMIT = 1000
OZON_MAX_IMAGES = 15
OZON_MAX_NAME_LENGTH = 255
OZON_MAX_DESCRIPTION_LENGTH = 6000

#: Import task item states that mean the marketplace refused the listing.
OZON_IMPORT_FAILED_STATES: frozenset[str] = frozenset({"failed", "declined", "error"})

_MONEY_SCALE = Decimal("0.01")

#: Ozon posting status -> ERP fulfillment status. Unknown tokens stay reserved: an
#: unmapped status must never silently release or deduct stock.
OZON_POSTING_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "awaiting_registration": FulfillmentStatus.PENDING_PAYMENT,
    "acceptance_in_progress": FulfillmentStatus.PENDING_PAYMENT,
    "awaiting_approve": FulfillmentStatus.CONFIRMED_RESERVED,
    "awaiting_packaging": FulfillmentStatus.CONFIRMED_RESERVED,
    "awaiting_verification": FulfillmentStatus.CONFIRMED_RESERVED,
    "awaiting_deliver": FulfillmentStatus.CONFIRMED_RESERVED,
    "client_arbitration": FulfillmentStatus.CONFIRMED_RESERVED,
    "driver_pickup": FulfillmentStatus.SHIPPED,
    "sent_by_seller": FulfillmentStatus.SHIPPED,
    "delivering": FulfillmentStatus.SHIPPED,
    "delivered": FulfillmentStatus.DELIVERED,
    "returned": FulfillmentStatus.REFUNDED,
    "cancelled": FulfillmentStatus.CANCELLED,
}

_CANCELLED_POSTING_STATUSES = frozenset({"cancelled", "returned"})


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; Ozon only speaks absolute instants."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso8601_z(value: datetime) -> str:
    """Ozon filter timestamp format, e.g. ``2026-11-20T00:00:00Z``."""
    utc = _as_utc(value)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}Z"


def _parse_iso8601(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _decimal(value: Any) -> Decimal:
    """Parse an Ozon money/quantity value into ``Decimal`` without touching ``float``."""
    if isinstance(value, Decimal):
        return value
    raw = "" if value is None else str(value).strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise AppError(
            slug="ozon-invalid-money",
            status_code=502,
            code="ERR_OZON_INVALID_MONEY",
            title="Invalid Ozon Money Value",
            detail=(
                "Ozon returned a non-numeric monetary value; refusing to coerce it into an "
                "amount."
            ),
        ) from None


def _money(value: Decimal) -> Decimal:
    """Quantize to the 2dp money scale with commercial rounding (never binary)."""
    return value.quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


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


def _country_code(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    return raw if len(raw) == 2 and raw.isascii() and raw.isalpha() else None


def verify_ozon_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    """Fail-closed constant-time HMAC-SHA256 check of ``x-ozon-signature``.

    Fails closed on a missing secret, a missing signature or an empty body: an unsigned
    delivery must never be treated as an authentic marketplace event.
    """
    signature = _header_value(headers, OZON_WEBHOOK_SIGNATURE_HEADER).strip().lower()
    if not secret or not signature or not raw_body:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class OzonAdapter(CommercePlatformAdapter):
    """Ozon Seller API adapter for one seller contract (``Client-Id`` + ``Api-Key``)."""

    def __init__(
        self,
        *,
        client_id: str,
        api_key: str,
        warehouse_id: str | int = "",
        fulfillment_mode: str = "fbs",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        mode = str(fulfillment_mode or "fbs").strip().lower()
        if mode not in OZON_FULFILLMENT_MODES:
            raise AppError(
                slug="ozon-fulfillment-mode-unsupported",
                status_code=422,
                code="ERR_OZON_FULFILLMENT_MODE",
                title="Unsupported Ozon Fulfillment Mode",
                detail=(
                    f"Ozon fulfillment mode '{fulfillment_mode}' is not supported; expected one "
                    f"of {', '.join(sorted(OZON_FULFILLMENT_MODES))}."
                ),
            )
        self.client_id = client_id
        self.warehouse_id = str(warehouse_id).strip()
        self.fulfillment_mode = mode
        self.endpoint_url = (endpoint_url or OZON_API_BASE).rstrip("/")
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"OzonAdapter(client_id={self.client_id!r}, "
            f"fulfillment_mode={self.fulfillment_mode!r}, "
            f"warehouse_id={self.warehouse_id!r}, endpoint_url={self.endpoint_url!r})"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout, trust_env=False)

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a log line or an ``AppError`` detail."""
        return (self._api_key,) if self._api_key else ()

    def _redacted(self, text: str, limit: int = 400) -> str:
        cleaned = text[:limit]
        for secret in self._secrets():
            cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned

    def _headers(self) -> dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _raise_api_error(self, response: httpx.Response, *, method: str, path: str) -> None:
        if response.status_code in (401, 403):
            raise AppError(
                slug="ozon-auth-failed",
                status_code=502,
                code="ERR_OZON_AUTH_FAILED",
                title="Ozon Authentication Failed",
                detail=(
                    "Ozon rejected the Client-Id/Api-Key pair for "
                    f"'{method} {path}' (HTTP {response.status_code})."
                ),
            )
        if response.status_code == 429:
            raise AppError(
                slug="ozon-rate-limited",
                status_code=429,
                code="ERR_OZON_RATE_LIMITED",
                title="Ozon Rate Limited",
                detail=(
                    f"Ozon returned HTTP 429 for '{method} {path}'; retry after the advertised "
                    "backoff."
                ),
            )
        raise AppError(
            slug="ozon-api-error",
            status_code=502,
            code="ERR_OZON_API_ERROR",
            title="Ozon API Error",
            detail=(
                f"Ozon rejected '{method} {path}' with HTTP {response.status_code}: "
                f"{self._redacted(response.text)}"
            ),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.request(
                    method,
                    f"{self.endpoint_url}{path}",
                    json=dict(body) if body is not None else None,
                    headers=self._headers(),
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="ozon-unreachable",
                    status_code=502,
                    code="ERR_OZON_UNREACHABLE",
                    title="Ozon Unreachable",
                    detail=(
                        f"Ozon Seller API call '{method} {path}' failed before any response "
                        "arrived."
                    ),
                ) from None
        if response.status_code >= 400:
            self._raise_api_error(response, method=method, path=path)
        return self._json_object(response)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        return _mapping(payload)

    # ------------------------------------------------------------------- order ingestion

    @property
    def order_list_path(self) -> str:
        """Ozon splits seller-warehouse (FBS) and Ozon-warehouse (FBO) postings."""
        return (
            OZON_FBS_ORDER_LIST_PATH
            if self.fulfillment_mode == "fbs"
            else OZON_FBO_ORDER_LIST_PATH
        )

    @staticmethod
    def _offset_cursor(cursor: str | None) -> int:
        raw = str(cursor or "").strip()
        if not raw:
            return 0
        if not raw.isdigit():
            raise AppError(
                slug="ozon-invalid-cursor",
                status_code=422,
                code="ERR_OZON_INVALID_CURSOR",
                title="Invalid Ozon Cursor",
                detail=(
                    "Ozon posting pagination is offset based; the cursor must be a non-negative "
                    f"integer (got '{raw}')."
                ),
            )
        return int(raw)

    @staticmethod
    def _normalize_line(product: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Ozon's ``price`` is the per-unit price; the line total is price x quantity."""
        offer_id = str(product.get("offer_id") or product.get("sku") or "").strip()
        if not offer_id:
            return None
        quantity = int(product.get("quantity") or 0)
        if quantity <= 0:
            return None
        unit_price = _decimal(product.get("price"))
        return NormalizedLineItem(
            external_sku=offer_id,
            title=str(product.get("name") or offer_id),
            quantity=quantity,
            line_total=_money(unit_price * Decimal(quantity)),
        )

    def _normalize_order(
        self, posting: Mapping[str, Any], *, fallback_updated_at: datetime
    ) -> NormalizedOrder | None:
        """Build one ``NormalizedOrder`` from a posting row, or ``None`` when unusable."""
        posting_number = str(posting.get("posting_number") or "").strip()
        if not posting_number:
            return None
        products = _mapping_rows(posting.get("products"))
        lines = [
            line for product in products if (line := self._normalize_line(product)) is not None
        ]
        if not lines:
            return None

        financial = _mapping(posting.get("financial_data"))
        financial_products = _mapping_rows(financial.get("products"))
        analytics = _mapping(posting.get("analytics_data"))
        status = str(posting.get("status") or "").strip()

        currency = ""
        for candidates in (
            [row.get("currency_code") for row in financial_products],
            [row.get("currency_code") for row in products],
            [analytics.get("currency")],
        ):
            for candidate in candidates:
                code = str(candidate or "").strip().upper()
                if len(code) == 3:
                    currency = code
                    break
            if currency:
                break
        if not currency:
            currency = "RUB"

        commission = Decimal("0")
        discount = Decimal("0")
        for row in financial_products:
            commission += _decimal(row.get("commission_amount"))
            discount += _decimal(row.get("total_discount_value"))
        commission = _money(commission)
        subtotal = _money(sum((line.line_total for line in lines), Decimal("0")))
        discount_amount = _money(discount)
        total_amount = _money(subtotal - discount_amount)
        if total_amount < Decimal("0"):
            total_amount = Decimal("0.00")
        refunded_amount = total_amount if status in _CANCELLED_POSTING_STATUSES else Decimal("0.00")

        metadata: dict[str, Any] = {
            "ozon_posting_number": posting_number,
            "ozon_order_id": posting.get("order_id"),
            "ozon_status": status,
            "ozon_fulfillment_mode": self.fulfillment_mode,
            "ozon_warehouse_id": self.warehouse_id or None,
            "ozon_delivery_method": posting.get("delivery_method"),
            "ozon_tracking_number": posting.get("tracking_number"),
            "ozon_is_express": analytics.get("is_express"),
            "ozon_shipment_date": posting.get("shipment_date"),
        }
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded_amount,
            platform_commission_base=commission,
        )

        customer_block = _mapping(posting.get("customer"))
        addressee = _mapping(posting.get("addressee"))
        email = str(customer_block.get("email") or addressee.get("email") or "").strip() or None
        phone = str(customer_block.get("phone") or addressee.get("phone") or "").strip() or None
        display_name = (
            str(customer_block.get("name") or addressee.get("name") or "").strip()
            or email
            or "Ozon Buyer"
        )
        external_customer_id = (
            str(customer_block.get("customer_id") or customer_block.get("id") or "").strip()
            or f"ozon-posting:{posting_number}"
        )

        updated_at = _parse_iso8601(
            posting.get("in_process_at") or posting.get("created_at")
        ) or fallback_updated_at

        return NormalizedOrder(
            external_order_id=posting_number,
            external_order_number=str(posting.get("order_number") or posting_number),
            order_mode=OrderMode.B2C_RETAIL,
            currency=currency,
            subtotal_amount=subtotal,
            discount_amount=discount_amount,
            total_amount=total_amount,
            platform_commission_base=commission,
            fulfillment_status=OZON_POSTING_STATUS_MAP.get(
                status.lower(), FulfillmentStatus.CONFIRMED_RESERVED
            ),
            payment_status=(
                PaymentStatus.REFUNDED
                if status.lower() in _CANCELLED_POSTING_STATUSES
                else PaymentStatus.PAID
            ),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone,
                external_customer_id=external_customer_id,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=display_name,
                country_code=_country_code(
                    addressee.get("country") or analytics.get("country")
                ),
            ),
            line_items=lines,
            channel_metadata=metadata,
            updated_at=updated_at,
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        since_utc = _as_utc(since)
        offset = self._offset_cursor(cursor)
        now = datetime.now(UTC)
        payload = await self._request(
            "POST",
            self.order_list_path,
            body={
                "dir": "ASC",
                "filter": {
                    "since": _iso8601_z(since_utc),
                    "to": _iso8601_z(now if now >= since_utc else since_utc),
                },
                "limit": OZON_ORDER_PAGE_LIMIT,
                "offset": offset,
                "with": {"analytics_data": True, "financial_data": True},
            },
        )
        result = _mapping(payload.get("result"))
        postings = _mapping_rows(result.get("postings"))
        orders = [
            order
            for posting in postings
            if (order := self._normalize_order(posting, fallback_updated_at=since_utc)) is not None
        ]
        if not postings:
            return orders, None
        has_next = bool(result.get("has_next"))
        return orders, (str(offset + len(postings)) if has_next else None)

    # ----------------------------------------------------------------- product ingestion

    @staticmethod
    def _product_variants(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Normalize Ozon ``sources`` rows into the shared variant shape."""
        variants: list[dict[str, Any]] = []
        for source in _mapping_rows(row.get("sources")):
            offer_id = str(source.get("offer_id") or source.get("sku") or "").strip()
            if not offer_id:
                continue
            variants.append(
                {
                    "external_sku": offer_id,
                    "external_variant_id": str(source.get("sku") or "") or None,
                    "price": _money(_decimal(source.get("price"))),
                    "currency": str(source.get("currency_code") or "RUB").upper(),
                    "quantity": max(0, int(source.get("stock") or 0)),
                }
            )
        return variants

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        since_utc = _as_utc(since)
        page = await self._request(
            "POST",
            OZON_PRODUCT_LIST_PATH,
            body={
                "filter": {"visibility": "ALL"},
                "last_id": str(cursor or "").strip(),
                "limit": OZON_PRODUCT_PAGE_LIMIT,
            },
        )
        result = _mapping(page.get("result"))
        rows = _mapping_rows(result.get("items"))
        product_ids = [
            str(row.get("product_id")).strip()
            for row in rows
            if str(row.get("product_id") or "").strip()
        ]
        if not product_ids:
            return [], None
        details = await self._request(
            "POST",
            OZON_PRODUCT_INFO_LIST_PATH,
            body={"product_id": product_ids},
        )
        detail_rows = _mapping_rows(details.get("items")) or _mapping_rows(
            _mapping(details.get("result")).get("items")
        )
        products: list[NormalizedProduct] = []
        for row in detail_rows:
            product_id = str(row.get("id") or row.get("product_id") or "").strip()
            if not product_id:
                continue
            updated_at = _parse_iso8601(row.get("updated_at") or row.get("created_at"))
            if updated_at is not None and updated_at < since_utc:
                continue
            products.append(
                NormalizedProduct(
                    external_product_id=product_id,
                    title=str(row.get("name") or row.get("offer_id") or product_id),
                    variants=self._product_variants(row),
                )
            )
        next_cursor = str(result.get("last_id") or "").strip() or None
        return products, next_cursor

    # ------------------------------------------------------------------- FBS fulfillment

    async def _posting_products(self, posting_number: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST",
            OZON_FBS_POSTING_GET_PATH,
            body={
                "posting_number": posting_number,
                "with": {"analytics_data": False, "financial_data": False},
            },
        )
        posting = _mapping(payload.get("result"))
        products: list[dict[str, Any]] = []
        for row in _mapping_rows(posting.get("products")):
            product_id = str(row.get("sku") or row.get("product_id") or "").strip()
            quantity = int(row.get("quantity") or 0)
            if not product_id or quantity <= 0:
                continue
            products.append({"product_id": int(product_id), "quantity": quantity})
        if not products:
            raise AppError(
                slug="ozon-posting-products-missing",
                status_code=502,
                code="ERR_OZON_POSTING_PRODUCTS_MISSING",
                title="Ozon Posting Products Missing",
                detail=(
                    f"Ozon posting '{posting_number}' returned no shippable products; refusing "
                    "to ship an empty package."
                ),
            )
        return products

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Ship one FBS posting through ``/v2/posting/fbs/ship``.

        Ozon FBO postings are picked, packed and shipped by Ozon, so they fail closed here
        instead of pretending the seller shipped them.
        """
        del notify_customer
        posting_number = str(external_order_id or "").strip()
        tracking = str(tracking_number or "").strip()
        carrier = str(carrier_company or "").strip()
        if self.fulfillment_mode != "fbs":
            raise AppError(
                slug="ozon-fulfillment-unsupported",
                status_code=422,
                code="ERR_OZON_FULFILLMENT_UNSUPPORTED",
                title="Ozon Fulfillment Unsupported",
                detail=(
                    "Ozon FBO postings are fulfilled by Ozon; only FBS postings can be shipped "
                    "by the seller."
                ),
            )
        if not posting_number or not tracking:
            raise AppError(
                slug="ozon-fulfillment-rejected",
                status_code=422,
                code="ERR_OZON_FULFILLMENT_REJECTED",
                title="Ozon Fulfillment Rejected",
                detail=(
                    "Ozon FBS shipping requires a non-empty posting number and tracking number."
                ),
            )

        package_id = str(fulfillment_order_id or "").strip()
        package: dict[str, Any] = {
            "products": await self._posting_products(posting_number) if not package_id else [],
            "tracking_number": tracking,
            "carrier": carrier,
        }
        if tracking_url:
            package["tracking_url"] = tracking_url.strip()
        if package_id:
            package["package_id"] = package_id

        payload = await self._request(
            "POST",
            OZON_FBS_SHIP_PATH,
            body={"posting_number": posting_number, "packages": [package]},
        )
        logger.info(
            "ozon_posting_shipped",
            posting_number=posting_number,
            carrier=carrier,
            tracking_number=tracking,
        )
        return {
            "channel": "ozon",
            "external_order_id": posting_number,
            "fulfillment_order_id": package_id or posting_number,
            "status": "shipped",
            "tracking_number": tracking,
            "carrier": carrier,
            "tracking_url": tracking_url,
            "raw": payload,
        }

    # -------------------------------------------------------------- listing publication

    @staticmethod
    def _listing_price(value: Any, *, label: str) -> Decimal:
        if isinstance(value, float):
            raise AppError(
                slug="ozon-invalid-money",
                status_code=422,
                code="ERR_OZON_INVALID_MONEY",
                title="Invalid Ozon Money Value",
                detail=(
                    f"Ozon listing price for '{label}' must be Decimal; IEEE-754 float prices "
                    "are refused so no binary rounding error can reach a live listing."
                ),
            )
        price = _money(_decimal(value))
        if price <= Decimal("0"):
            raise AppError(
                slug="ozon-listing-rejected",
                status_code=422,
                code="ERR_OZON_LISTING_REJECTED",
                title="Ozon Listing Rejected",
                detail=f"Ozon listing price for '{label}' must be greater than zero.",
            )
        return price

    @staticmethod
    def _compose_description(payload: MarketplaceListingPayload) -> str:
        """Render merchandising context Ozon has no structured field for (options, MOQ)."""
        blocks: list[str] = [payload.description.strip()]
        bullets = [point.strip() for point in payload.bullet_points if point.strip()]
        if bullets:
            blocks.append("\n".join(f"- {point}" for point in bullets))
        details: list[str] = []
        if payload.category.strip():
            details.append(f"Category: {payload.category.strip()}")
        if payload.hs_code:
            details.append(f"HS code: {payload.hs_code.strip()}")
        for tier in payload.moq_tiers:
            details.append(f"MOQ {tier.min_qty}: {_money(tier.unit_price_usd)} per unit")
        for option in payload.options:
            values = ", ".join(value.strip() for value in option.values if value.strip())
            if values:
                details.append(f"Option {option.name.strip()}: {values}")
        for variant in payload.variants:
            pairs = ", ".join(
                f"{key.strip()}={value.strip()}"
                for key, value in variant.option_values.items()
                if str(value).strip()
            )
            details.append(f"Variant {variant.external_sku.strip()}: {pairs or 'default'}")
        if details:
            blocks.append("\n".join(details))
        return "\n\n".join(block for block in blocks if block)[:OZON_MAX_DESCRIPTION_LENGTH]

    @staticmethod
    def _variant_name(title: str, option_values: Mapping[str, str]) -> str:
        suffix = " / ".join(
            str(value).strip() for value in option_values.values() if str(value).strip()
        )
        name = f"{title} ({suffix})" if suffix else title
        return name[:OZON_MAX_NAME_LENGTH]

    def _listing_items(
        self,
        payload: MarketplaceListingPayload,
        *,
        clean_sku: str,
        title: str,
        gallery: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Build the ``/v3/product/import`` item list (one item per variant)."""
        items: list[dict[str, Any]] = []
        variants = list(payload.variants)
        if not variants:
            images = list(gallery)
            item: dict[str, Any] = {
                "offer_id": clean_sku,
                "name": title[:OZON_MAX_NAME_LENGTH],
                "price": str(self._listing_price(payload.price_usd, label=clean_sku)),
                "currency_code": OZON_LISTING_CURRENCY,
                "vat": "0",
                "images": images,
            }
            if images:
                item["primary_image"] = images[0]
            return [item]

        for index, variant in enumerate(variants, start=1):
            offer_id = (variant.external_sku or f"{clean_sku}-{index}").strip()
            if not offer_id:
                raise AppError(
                    slug="ozon-listing-rejected",
                    status_code=422,
                    code="ERR_OZON_LISTING_REJECTED",
                    title="Ozon Listing Rejected",
                    detail="Every Ozon variant requires a non-empty external SKU (offer_id).",
                )
            images = list(gallery)
            image_url = (variant.image_url or "").strip()
            if image_url.startswith("https://"):
                images.insert(0, image_url)
            item = {
                "offer_id": offer_id,
                "name": self._variant_name(title, variant.option_values),
                "price": str(self._listing_price(variant.price_usd, label=offer_id)),
                "currency_code": OZON_LISTING_CURRENCY,
                "vat": "0",
                "images": images[:OZON_MAX_IMAGES],
            }
            if images:
                item["primary_image"] = images[0]
            items.append(item)
        return items

    async def _resolve_import_task(
        self, task_id: Any, *, clean_sku: str
    ) -> list[Mapping[str, Any]]:
        """Poll ``/v1/product/import/info`` once and return the per-item import states."""
        raw_task = str(task_id if task_id is not None else "").strip()
        if not raw_task.isdigit():
            raise AppError(
                slug="ozon-listing-rejected",
                status_code=502,
                code="ERR_OZON_LISTING_REJECTED",
                title="Ozon Listing Rejected",
                detail=f"Ozon returned no usable import task_id for '{clean_sku}'.",
            )
        payload = await self._request(
            "POST",
            OZON_PRODUCT_IMPORT_INFO_PATH,
            body={"task_id": int(raw_task)},
        )
        result = _mapping(payload.get("result"))
        rows = _mapping_rows(result.get("items"))
        if not rows:
            raise AppError(
                slug="ozon-listing-unconfirmed",
                status_code=502,
                code="ERR_OZON_LISTING_UNCONFIRMED",
                title="Ozon Listing Unconfirmed",
                detail=(
                    f"Ozon import task {raw_task} for '{clean_sku}' returned no item states; "
                    "refusing to persist a live listing the marketplace did not confirm."
                ),
            )
        return rows

    async def _push_description(
        self, items: Sequence[Mapping[str, Any]], description: str
    ) -> dict[str, Any]:
        """Attach the merchandising copy Ozon's import endpoint does not accept."""
        return await self._request(
            "POST",
            OZON_PRODUCT_UPDATE_PATH,
            body={
                "items": [
                    {
                        "offer_id": str(item.get("offer_id") or "").strip(),
                        "name": str(item.get("name") or ""),
                        "description": description,
                    }
                    for item in items
                ]
            },
        )

    async def _push_pictures(
        self, product_id: str, blobs: Sequence[MarketplaceImageBlob]
    ) -> dict[str, Any]:
        data_uris = [
            "data:{};base64,{}".format(
                blob.mime_type, base64.b64encode(blob.data).decode("ascii")
            )
            for blob in blobs
            if blob.data
        ]
        return await self._request(
            "POST",
            OZON_PICTURE_IMPORT_PATH,
            body={"product_id": int(product_id), "images": data_uris},
        )

    async def _push_stock_batch(
        self,
        resolved: Sequence[tuple[str, str, int]],
        *,
        warehouse_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            OZON_STOCK_IMPORT_PATH,
            body={
                "stocks": [
                    {
                        "offer_id": offer_id,
                        "product_id": int(product_id),
                        "stock": quantity,
                        "warehouse_id": int(warehouse_id),
                    }
                    for offer_id, product_id, quantity in resolved
                ]
            },
        )

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create a live Ozon listing: import -> confirm -> copy -> pictures -> stock."""
        clean_sku = payload.external_sku.strip()
        title = payload.title.strip()
        if not clean_sku or not title:
            raise AppError(
                slug="ozon-listing-rejected",
                status_code=422,
                code="ERR_OZON_LISTING_REJECTED",
                title="Ozon Listing Rejected",
                detail="Ozon listing requires a non-empty SKU and title.",
            )
        gallery = [
            url.strip()
            for url in payload.images
            if url.strip().startswith("https://")
        ][:OZON_MAX_IMAGES]
        items = self._listing_items(
            payload, clean_sku=clean_sku, title=title, gallery=gallery
        )
        quantities = {
            str(item.get("offer_id") or ""): quantity
            for item, quantity in zip(
                items,
                (
                    [max(0, int(payload.quantity))]
                    if not payload.variants
                    else [max(0, int(variant.quantity)) for variant in payload.variants]
                ),
                strict=True,
            )
        }

        import_payload = await self._request(
            "POST", OZON_PRODUCT_IMPORT_PATH, body={"items": items}
        )
        task_id = _mapping(import_payload.get("result")).get("task_id")
        rows = await self._resolve_import_task(task_id, clean_sku=clean_sku)

        variant_receipts: list[MarketplaceVariantReceipt] = []
        resolved: list[tuple[str, str, int]] = []
        for row in rows:
            offer_id = str(row.get("offer_id") or clean_sku).strip()
            state = str(row.get("status") or "").strip().lower()
            errors = row.get("errors")
            if state in OZON_IMPORT_FAILED_STATES or errors:
                raise AppError(
                    slug="ozon-listing-rejected",
                    status_code=502,
                    code="ERR_OZON_LISTING_REJECTED",
                    title="Ozon Listing Rejected",
                    detail=self._redacted(
                        json.dumps({"offer_id": offer_id, "status": state, "errors": errors})
                    ),
                )
            product_id = str(row.get("product_id") or "").strip()
            if not product_id:
                raise AppError(
                    slug="ozon-listing-unconfirmed",
                    status_code=502,
                    code="ERR_OZON_LISTING_UNCONFIRMED",
                    title="Ozon Listing Unconfirmed",
                    detail=(
                        f"Ozon import task for offer_id '{offer_id}' returned no product_id; "
                        "refusing to persist an unconfirmed live listing."
                    ),
                )
            variant_receipts.append(
                MarketplaceVariantReceipt(
                    external_sku=offer_id,
                    external_variant_id=str(row.get("sku") or "").strip() or product_id,
                    external_inventory_item_id=None,
                )
            )
            resolved.append((offer_id, product_id, quantities.get(offer_id, 0)))

        logger.info(
            "ozon_listing_imported",
            external_sku=clean_sku,
            variants=len(resolved),
            task_id=str(task_id),
        )

        await self._push_description(items, self._compose_description(payload))

        if payload.image_blobs:
            for _, product_id, _ in resolved:
                await self._push_pictures(product_id, payload.image_blobs)

        stock_pushed = False
        if self.warehouse_id:
            stock_targets = [(offer, pid, qty) for offer, pid, qty in resolved if qty > 0]
            if stock_targets:
                await self._push_stock_batch(stock_targets, warehouse_id=self.warehouse_id)
                stock_pushed = True

        return MarketplaceListingDispatchReceipt(
            external_product_id=resolved[0][1],
            external_sku=clean_sku,
            status=str(rows[0].get("status") or "imported").strip().lower(),
            raw_response={
                "task_id": task_id,
                "items": [dict(row) for row in rows],
                "stock_pushed": stock_pushed,
            },
            variant_receipts=variant_receipts,
        )

    # ------------------------------------------------------------------ inventory push

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
        """Push one absolute stock level to ``/v1/product/import/stocks``."""
        del external_inventory_item_id
        clean_sku = (external_sku or "").strip()
        if not clean_sku:
            raise AppError(
                slug="ozon-inventory-sync-failed",
                status_code=422,
                code="ERR_OZON_INVENTORY_SYNC",
                title="Ozon Inventory Sync Failed",
                detail="Ozon inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        target_warehouse = (warehouse_id or self.warehouse_id or "").strip()
        if not target_warehouse:
            raise AppError(
                slug="ozon-inventory-sync-failed",
                status_code=422,
                code="ERR_OZON_INVENTORY_SYNC",
                title="Ozon Inventory Sync Failed",
                detail=(
                    "Ozon inventory sync requires an warehouse_id (Ozon stocks are per "
                    "warehouse)."
                ),
            )
        if not target_warehouse.isdigit():
            raise AppError(
                slug="ozon-inventory-sync-failed",
                status_code=422,
                code="ERR_OZON_INVENTORY_SYNC",
                title="Ozon Inventory Sync Failed",
                detail=(
                    "Ozon warehouse_id must be the numeric Ozon warehouse identifier "
                    f"(got '{target_warehouse}')."
                ),
            )

        product_id = (external_product_id or external_variant_id or "").strip()
        stock_row: dict[str, Any] = {
            "offer_id": clean_sku,
            "stock": norm_qty,
            "warehouse_id": int(target_warehouse),
        }
        if product_id.isdigit():
            stock_row["product_id"] = int(product_id)
        payload = await self._request(
            "POST", OZON_STOCK_IMPORT_PATH, body={"stocks": [stock_row]}
        )
        result = _mapping(payload.get("result"))
        updated = result.get("updated")
        if updated is False:
            raise AppError(
                slug="ozon-inventory-sync-failed",
                status_code=502,
                code="ERR_OZON_INVENTORY_SYNC",
                title="Ozon Inventory Sync Failed",
                detail=(
                    f"Ozon refused the stock update for '{clean_sku}' "
                    f"({self._redacted(json.dumps(result))})."
                ),
            )
        return {
            "channel": "ozon",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "product_id": product_id or None,
            "variant_id": (external_variant_id or "").strip() or None,
            "warehouse_id": target_warehouse,
            "location_id": (location_id or "").strip() or None,
            "raw": payload,
        }

    # -------------------------------------------------------------------- webhook check

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_ozon_webhook(raw_body, headers, secret)


__all__ = [
    "OZON_API_BASE",
    "OZON_FBO_ORDER_LIST_PATH",
    "OZON_FBS_ORDER_LIST_PATH",
    "OZON_FBS_SHIP_PATH",
    "OZON_LISTING_CURRENCY",
    "OZON_PICTURE_IMPORT_PATH",
    "OZON_PRODUCT_IMPORT_INFO_PATH",
    "OZON_PRODUCT_IMPORT_PATH",
    "OZON_PRODUCT_INFO_LIST_PATH",
    "OZON_PRODUCT_LIST_PATH",
    "OZON_PRODUCT_UPDATE_PATH",
    "OZON_STOCK_IMPORT_PATH",
    "OZON_WEBHOOK_SIGNATURE_HEADER",
    "OzonAdapter",
    "verify_ozon_webhook",
]
