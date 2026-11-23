"""Mercado Libre connector: multi-site order sync, shipments, item publish, webhooks.

Everything here is deterministic and offline-testable: the adapter owns an
``httpx.MockTransport``-compatible ``transport`` seam, every monetary amount is parsed into
``Decimal`` (never ``float``) and quantized with ``ROUND_HALF_UP``, and neither the
``access_token`` nor the ``refresh_token`` can reach a log record, an ``AppError`` detail or
``repr()``.

Mercado Libre has no single "order" API shared by every flow, so the connector pins the
documented resources explicitly:

* orders   — ``GET /orders/search?seller=...&order.date_last_updated.from=...``
* products — ``GET /users/{seller_id}/items/search`` + ``GET /items?ids=`` (20 ids per call)
* shipment — ``PUT /shipments/{shipment_id}`` (tracking is carrier-owned in ``full`` mode)
* items    — ``POST /items`` (multi-variation) + ``PUT /items/{id}/description``
* stock    — ``PUT /items/{id}`` or ``PUT /items/{id}/variations/{variation_id}``
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NoReturn
from urllib.parse import quote

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

MERCADO_LIBRE_API_BASE_URL = "https://api.mercadolibre.com"
MERCADO_LIBRE_ORDERS_SEARCH_PATH = "/orders/search"
MERCADO_LIBRE_ORDERS_PATH = "/orders"
MERCADO_LIBRE_ITEMS_PATH = "/items"
MERCADO_LIBRE_SHIPMENTS_PATH = "/shipments"

#: Mercado Libre caps every paged search at 50 rows.
MERCADO_LIBRE_MAX_PAGE_LIMIT = 50
#: ``GET /items?ids=`` accepts at most 20 item ids per multi-get call.
MERCADO_LIBRE_MAX_IDS_PER_MULTIGET = 20
#: Mercado Libre truncates (and rejects above) 60 characters for an item title.
MERCADO_LIBRE_MAX_TITLE_LENGTH = 60
#: An item carries at most 100 variations, each with at most 2 attribute combinations.
MERCADO_LIBRE_MAX_VARIATIONS = 100
MERCADO_LIBRE_MAX_VARIATION_ATTRIBUTES = 2
#: Mercado Libre accepts at most 12 pictures per item.
MERCADO_LIBRE_MAX_PICTURES = 12
#: Leaf category used only when the catalog category is not already a Mercado Libre id.
MERCADO_LIBRE_DEFAULT_CATEGORY_ID = "MLM1051"
MERCADO_LIBRE_LISTING_TYPE_ID = "gold_special"

MONEY_SCALE = Decimal("0.01")

#: ``fulfillment_mode`` values the connector accepts. ``full`` means Mercado Envíos Full,
#: where Mercado Libre (not the seller) owns the carrier assignment and tracking push.
MERCADO_LIBRE_FULFILLMENT_MODES: frozenset[str] = frozenset({"standard", "full"})

#: Mercado Libre site -> currency the item/order is priced in.
SITE_CURRENCIES: Mapping[str, str] = {
    "MLM": "MXN",
    "MLB": "BRL",
    "MLA": "ARS",
    "MCO": "COP",
    "MLC": "CLP",
    "MLU": "UYU",
    "MPE": "PEN",
    "MEC": "USD",
}

#: A real Mercado Libre leaf category id looks like ``MLM1051``; catalog names ("LED
#: Lighting") must never be sent as ``category_id``.
_CATEGORY_ID_PATTERN = re.compile(r"^ML[A-Z]\d{1,8}$")

_ZERO = Decimal("0.00")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return list(value)
    return []


def _header(headers: Mapping[str, str], *names: str) -> str:
    """Read the first present header, tolerating any client-side casing."""
    for name in names:
        value = headers.get(name)
        if value not in (None, ""):
            return str(value).strip()
    lowered = {str(key).strip().lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name.strip().lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _signature_tokens(value: str) -> tuple[str, ...]:
    """Split ``v1=<hex>``/``ts=<unix>,v1=<hex>``/bare-digest signature headers."""
    tokens: list[str] = []
    bare = value.strip()
    if bare:
        tokens.append(bare)
    for part in value.split(","):
        _key, _sep, raw = part.strip().partition("=")
        candidate = raw.strip()
        if candidate and candidate not in tokens:
            tokens.append(candidate)
    return tuple(tokens)


def verify_mercado_libre_webhook(
    raw_body: bytes, headers: Mapping[str, str], secret: str
) -> bool:
    """Fail-closed constant-time HMAC-SHA256 check of ``x-signature``/``x-meli-signature``.

    Mercado Libre ships notifications with either header and has used both a bare hex
    digest, a base64 digest and a ``ts=...,v1=<hex>`` composite, so every documented shape
    is compared with ``hmac.compare_digest``. An empty secret, body or signature is a hard
    ``False`` — a webhook is never trusted by default.
    """
    signature = _header(headers, "x-signature", "x-meli-signature")
    if not signature or not secret or not raw_body:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    hex_digest = digest.hex()
    b64_digest = base64.b64encode(digest).decode()
    for token in _signature_tokens(signature):
        if hmac.compare_digest(hex_digest, token.lower()):
            return True
        if hmac.compare_digest(b64_digest, token) or hmac.compare_digest(
            b64_digest.rstrip("="), token
        ):
            return True
    return False


class MercadoLibreAdapter(CommercePlatformAdapter):
    """Mercado Libre connector for one seller account in one site (``MLM``, ``MLB``, ...).

    The OAuth ``access_token`` is held privately; ``refresh_token`` is kept only so the
    credential vault can rotate it and is never used to call the API (Mercado Libre's
    refresh exchange belongs to the token-refresh job, not to a per-request adapter path).
    """

    def __init__(
        self,
        *,
        seller_id: str,
        access_token: str,
        site_id: str = "MLM",
        refresh_token: str = "",
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        seller = str(seller_id or "").strip()
        if not seller:
            raise AppError(
                slug="mercado-libre-seller-id-required",
                status_code=422,
                code="ERR_MERCADO_LIBRE_SELLER_ID_REQUIRED",
                title="Mercado Libre Seller ID Required",
                detail="A Mercado Libre adapter needs the numeric seller id it syncs.",
            )
        token = str(access_token or "").strip()
        if not token:
            raise AppError(
                slug="mercado-libre-access-token-required",
                status_code=422,
                code="ERR_MERCADO_LIBRE_ACCESS_TOKEN_REQUIRED",
                title="Mercado Libre Access Token Required",
                detail="A Mercado Libre adapter needs a non-empty OAuth access token.",
            )
        mode = str(fulfillment_mode or "").strip().lower() or "standard"
        if mode not in MERCADO_LIBRE_FULFILLMENT_MODES:
            raise AppError(
                slug="mercado-libre-fulfillment-mode-invalid",
                status_code=422,
                code="ERR_MERCADO_LIBRE_FULFILLMENT_MODE_INVALID",
                title="Mercado Libre Fulfillment Mode Invalid",
                detail=(
                    f"Unsupported fulfillment_mode '{fulfillment_mode}'; expected one of "
                    f"{', '.join(sorted(MERCADO_LIBRE_FULFILLMENT_MODES))}."
                ),
            )
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise AppError(
                slug="mercado-libre-timeout-invalid",
                status_code=422,
                code="ERR_MERCADO_LIBRE_TIMEOUT_INVALID",
                title="Mercado Libre Timeout Invalid",
                detail="The Mercado Libre HTTP timeout must be greater than zero seconds.",
            )
        self.seller_id = seller
        self.site_id = str(site_id or "").strip().upper() or "MLM"
        self.currency_id = SITE_CURRENCIES.get(self.site_id, "USD")
        self.fulfillment_mode = mode
        self.endpoint_url = str(endpoint_url or MERCADO_LIBRE_API_BASE_URL).rstrip("/")
        self._access_token = token
        self._refresh_token = str(refresh_token or "").strip()
        self._transport = transport
        self._timeout_seconds = timeout

    def __repr__(self) -> str:
        return (
            f"MercadoLibreAdapter(seller_id={self.seller_id!r}, site_id={self.site_id!r}, "
            f"fulfillment_mode={self.fulfillment_mode!r}, endpoint_url={self.endpoint_url!r})"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout_seconds, trust_env=False
        )

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never surface in a log line or an ``AppError`` detail."""
        return tuple(
            value for value in (self._access_token, self._refresh_token) if value
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
        """Map the credential/rate outcomes Mercado Libre expects callers to handle."""
        if response.status_code in (401, 403):
            raise AppError(
                slug="mercado-libre-auth-failed",
                status_code=502,
                code="ERR_MERCADO_LIBRE_AUTH_FAILED",
                title="Mercado Libre Authentication Failed",
                detail=(
                    "Mercado Libre rejected the OAuth access token for the seller account "
                    f"(HTTP {response.status_code})."
                ),
            )
        if response.status_code == 429:
            raise AppError(
                slug="mercado-libre-rate-limited",
                status_code=429,
                code="ERR_MERCADO_LIBRE_RATE_LIMITED",
                title="Mercado Libre Rate Limited",
                detail=(
                    "Mercado Libre returned HTTP 429 for the call; retry after the advertised "
                    "backoff."
                ),
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with self._client() as client:
            try:
                response = await client.request(
                    method,
                    f"{self.endpoint_url}{path}",
                    params=dict(params) if params is not None else None,
                    json=dict(json_body) if json_body is not None else None,
                    headers=headers,
                )
            except httpx.HTTPError:
                raise AppError(
                    slug="mercado-libre-unreachable",
                    status_code=502,
                    code="ERR_MERCADO_LIBRE_UNREACHABLE",
                    title="Mercado Libre Unreachable",
                    detail=(
                        f"Mercado Libre call '{method} {path}' failed before any response "
                        "arrived."
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

    @staticmethod
    def _json_list(response: httpx.Response) -> list[Any]:
        if not response.content:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        return payload if isinstance(payload, list) else []

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
        """Parse a Mercado Libre money value into 2dp ``Decimal``; never a ``float``."""
        if value is None or value == "":
            return _ZERO.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        try:
            return Decimal(str(value)).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        except (ArithmeticError, ValueError):
            raise AppError(
                slug="mercado-libre-invalid-money",
                status_code=502,
                code="ERR_MERCADO_LIBRE_INVALID_MONEY",
                title="Invalid Mercado Libre Money Value",
                detail=(
                    "Mercado Libre returned a monetary value that is not a finite decimal "
                    "number."
                ),
            ) from None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _parse_optional_datetime(cls, value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @classmethod
    def _parse_datetime(cls, value: Any) -> datetime:
        return cls._parse_optional_datetime(value) or datetime.now(UTC)

    @classmethod
    def _format_datetime(cls, value: datetime) -> str:
        """Mercado Libre filter form: ``2024-05-01T00:00:00.000-00:00`` (UTC)."""
        moment = cls._as_utc(value)
        return (
            moment.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{moment.microsecond // 1000:03d}-00:00"
        )

    @staticmethod
    def _parse_offset(cursor: str | None) -> int:
        if cursor is None or not str(cursor).strip():
            return 0
        try:
            return max(int(str(cursor).strip()), 0)
        except ValueError:
            raise AppError(
                slug="mercado-libre-invalid-cursor",
                status_code=422,
                code="ERR_MERCADO_LIBRE_INVALID_CURSOR",
                title="Invalid Mercado Libre Cursor",
                detail="Mercado Libre offset cursors are non-negative integers.",
            ) from None

    @staticmethod
    def _next_cursor(*, offset: int, fetched: int, total: Any) -> str | None:
        """Advance the offset cursor while Mercado Libre reports more rows than we read."""
        if fetched <= 0:
            return None
        advanced = offset + fetched
        total_count = MercadoLibreAdapter._as_int(total, -1)
        if total_count < 0:
            # No ``paging`` block: a short page is the only honest end-of-feed signal.
            return str(advanced) if fetched >= MERCADO_LIBRE_MAX_PAGE_LIMIT else None
        return str(advanced) if total_count > advanced else None

    @staticmethod
    def _paging_total(payload: Mapping[str, Any]) -> Any:
        return _mapping(payload.get("paging")).get("total")

    # ---------------------------------------------------------------------- order mapping

    def _normalize_line_item(self, row: Mapping[str, Any]) -> NormalizedLineItem | None:
        item = _mapping(row.get("item"))
        sku = (
            str(item.get("seller_sku") or "").strip()
            or str(item.get("seller_custom_field") or "").strip()
            or str(item.get("id") or "").strip()
        )
        if not sku:
            return None
        quantity = max(self._as_int(row.get("quantity"), 1), 1)
        unit_price = self._as_decimal(row.get("unit_price"))
        line_total = (unit_price * Decimal(quantity)).quantize(
            MONEY_SCALE, rounding=ROUND_HALF_UP
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(item.get("title") or sku).strip() or sku,
            quantity=quantity,
            line_total=line_total,
        )

    def _order_item_fee(self, row: Mapping[str, Any]) -> Decimal:
        """The Mercado Libre sale fee is reported per order item as ``sale_fee``."""
        return self._as_decimal(row.get("sale_fee"))

    def _tax_amount(self, order: Mapping[str, Any]) -> Decimal:
        taxes = order.get("taxes")
        if isinstance(taxes, Mapping):
            return self._as_decimal(taxes.get("amount"))
        total = _ZERO
        for row in _sequence(taxes):
            if isinstance(row, Mapping):
                total += self._as_decimal(row.get("amount"))
        return total.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)

    def _refunded_amount(self, order: Mapping[str, Any], *, canceled: bool) -> Decimal:
        total = _ZERO
        for payment in _sequence(order.get("payments")):
            if not isinstance(payment, Mapping):
                continue
            if str(payment.get("status") or "").strip().lower() != "refunded":
                continue
            total += self._as_decimal(payment.get("transaction_amount"))
        total = total.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        if canceled and total <= _ZERO:
            # A cancelled order with no explicit refund row was refunded in full.
            return self._as_decimal(order.get("total_amount"))
        return total

    @staticmethod
    def _order_tags(order: Mapping[str, Any]) -> set[str]:
        return {
            str(tag).strip().lower() for tag in _sequence(order.get("tags")) if str(tag).strip()
        }

    def _fulfillment_status(self, order: Mapping[str, Any]) -> FulfillmentStatus:
        status = str(order.get("status") or "").strip().lower()
        if status in {"cancelled", "invalid"}:
            return FulfillmentStatus.CANCELLED
        tags = self._order_tags(order)
        if "delivered" in tags:
            return FulfillmentStatus.DELIVERED
        if "shipped" in tags:
            return FulfillmentStatus.SHIPPED
        return FulfillmentStatus.CONFIRMED_RESERVED

    @staticmethod
    def _payment_status(order: Mapping[str, Any]) -> PaymentStatus:
        status = str(order.get("status") or "").strip().lower()
        if status in {"cancelled", "invalid"}:
            return PaymentStatus.REFUNDED
        if status in {"partially_paid", "partially_refunded"}:
            return PaymentStatus.PARTIALLY_PAID
        if status == "paid":
            return PaymentStatus.PAID
        return PaymentStatus.UNPAID

    @staticmethod
    def _phone_e164(buyer: Mapping[str, Any]) -> str | None:
        phone = _mapping(buyer.get("phone"))
        area = "".join(ch for ch in str(phone.get("area_code") or "") if ch.isdigit())
        number = "".join(ch for ch in str(phone.get("number") or "") if ch.isdigit())
        if not number:
            return None
        return f"+{area}{number}" if area else f"+{number}"

    def _normalize_order(self, order: Mapping[str, Any]) -> NormalizedOrder | None:
        external_order_id = str(order.get("id") or "").strip()
        if not external_order_id:
            return None

        line_items: list[NormalizedLineItem] = []
        commission = _ZERO
        for row in _sequence(order.get("order_items")):
            if not isinstance(row, Mapping):
                continue
            line = self._normalize_line_item(row)
            if line is None:
                continue
            line_items.append(line)
            commission += self._order_item_fee(row)
        if not line_items:
            return None
        commission = commission.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)

        subtotal = sum((line.line_total for line in line_items), start=_ZERO).quantize(
            MONEY_SCALE, rounding=ROUND_HALF_UP
        )
        shipping = self._as_decimal(order.get("shipping_cost"))
        tax = self._tax_amount(order)
        declared_total = order.get("total_amount")
        total = (
            self._as_decimal(declared_total)
            if declared_total not in (None, "")
            else (subtotal + shipping + tax).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        )

        fulfillment_status = self._fulfillment_status(order)
        canceled = fulfillment_status is FulfillmentStatus.CANCELLED
        refunded = self._refunded_amount(order, canceled=canceled)

        buyer = _mapping(order.get("buyer"))
        billing = _mapping(buyer.get("billing_info"))
        nickname = str(buyer.get("nickname") or "").strip()
        email = str(buyer.get("email") or "").strip().lower() or None
        full_name = " ".join(
            part
            for part in (
                str(billing.get("name") or "").strip(),
                str(billing.get("last_name") or "").strip(),
            )
            if part
        )
        buyer_id = str(buyer.get("id") or "").strip() or None
        shipment_id = str(_mapping(order.get("shipping")).get("id") or "").strip()
        tags = self._order_tags(order)

        metadata: dict[str, Any] = {
            "mercado_libre_order_id": external_order_id,
            "mercado_libre_seller_id": self.seller_id,
            "mercado_libre_site_id": self.site_id,
            "mercado_libre_order_status": order.get("status"),
            "mercado_libre_status_detail": order.get("status_detail"),
            "mercado_libre_tags": sorted(tags) or None,
        }
        if shipment_id:
            metadata["mercado_libre_shipment_id"] = shipment_id
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )
        return NormalizedOrder(
            external_order_id=external_order_id,
            external_order_number=external_order_id,
            order_mode=OrderMode.B2C_RETAIL,
            currency=str(order.get("currency_id") or self.currency_id).strip().upper()
            or self.currency_id,
            fx_rate_to_base=None,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            tax_collected=tax,
            total_amount=total,
            platform_commission_base=commission.quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=self._payment_status(order),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=self._phone_e164(buyer),
                external_customer_id=buyer_id,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=full_name or nickname or email or "Mercado Libre Buyer",
                country_code=None,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=self._parse_datetime(
                order.get("last_updated") or order.get("date_closed") or order.get("date_created")
            ),
        )

    # ---------------------------------------------------------------- CommercePlatformAdapter

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        offset = self._parse_offset(cursor)
        response = await self._request(
            "GET",
            MERCADO_LIBRE_ORDERS_SEARCH_PATH,
            params={
                "seller": self.seller_id,
                "order.date_last_updated.from": self._format_datetime(since),
                "sort": "date_desc",
                "limit": MERCADO_LIBRE_MAX_PAGE_LIMIT,
                "offset": offset,
            },
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="mercado-libre-order-sync-failed",
                code="ERR_MERCADO_LIBRE_ORDER_SYNC_FAILED",
                title="Mercado Libre Order Sync Failed",
            )
        payload = self._json_object(response)
        rows = _sequence(payload.get("results"))
        orders = [
            order
            for row in rows
            if isinstance(row, Mapping) and (order := self._normalize_order(row)) is not None
        ]
        # The offset counts raw order rows, so the cursor advances by what the feed returned
        # even when a row without usable line items is skipped.
        return orders, self._next_cursor(
            offset=offset, fetched=len(rows), total=self._paging_total(payload)
        )

    async def _fetch_item_details(self, item_ids: Sequence[str]) -> list[Mapping[str, Any]]:
        response = await self._request(
            "GET",
            MERCADO_LIBRE_ITEMS_PATH,
            params={"ids": ",".join(item_ids)},
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="mercado-libre-product-sync-failed",
                code="ERR_MERCADO_LIBRE_PRODUCT_SYNC_FAILED",
                title="Mercado Libre Product Sync Failed",
            )
        details: list[Mapping[str, Any]] = []
        for row in self._json_list(response):
            if not isinstance(row, Mapping):
                continue
            if self._as_int(row.get("code"), 200) >= 400:
                continue
            body = row.get("body")
            if isinstance(body, Mapping):
                details.append(body)
        return details

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        # The items search has no reliable modified-since filter, so the feed is walked with
        # limit/offset and each item's own ``last_updated`` is compared with ``since``.
        offset = self._parse_offset(cursor)
        response = await self._request(
            "GET",
            f"/users/{quote(self.seller_id, safe='')}/items/search",
            params={
                "status": "active",
                "sort": "last_updated_desc",
                "limit": MERCADO_LIBRE_MAX_PAGE_LIMIT,
                "offset": offset,
            },
        )
        if response.status_code >= 400:
            self._raise_api_error(
                response,
                slug="mercado-libre-product-sync-failed",
                code="ERR_MERCADO_LIBRE_PRODUCT_SYNC_FAILED",
                title="Mercado Libre Product Sync Failed",
            )
        payload = self._json_object(response)
        item_ids = [
            str(row).strip() for row in _sequence(payload.get("results")) if str(row).strip()
        ]
        boundary = self._as_utc(since)
        products: list[NormalizedProduct] = []
        for start in range(0, len(item_ids), MERCADO_LIBRE_MAX_IDS_PER_MULTIGET):
            chunk = item_ids[start : start + MERCADO_LIBRE_MAX_IDS_PER_MULTIGET]
            for body in await self._fetch_item_details(chunk):
                product = self._normalize_product(body)
                if product is None:
                    continue
                updated = self._parse_optional_datetime(body.get("last_updated"))
                if updated is not None and updated < boundary:
                    continue
                products.append(product)
        return products, self._next_cursor(
            offset=offset, fetched=len(item_ids), total=self._paging_total(payload)
        )

    def _normalize_product(self, body: Mapping[str, Any]) -> NormalizedProduct | None:
        item_id = str(body.get("id") or "").strip()
        if not item_id:
            return None
        title = str(body.get("title") or item_id).strip() or item_id
        base_sku = (
            str(body.get("seller_custom_field") or "").strip()
            or str(body.get("seller_sku") or "").strip()
            or item_id
        )
        pictures = self._picture_urls_of(body)
        variants: list[dict[str, Any]] = []
        for variation in _sequence(body.get("variations")):
            if not isinstance(variation, Mapping):
                continue
            variation_id = str(variation.get("id") or "").strip() or None
            option_values = {
                str(_mapping(combo).get("name") or "").strip(): str(
                    _mapping(combo).get("value_name") or ""
                ).strip()
                for combo in _sequence(variation.get("attribute_combinations"))
                if str(_mapping(combo).get("name") or "").strip()
            }
            variants.append(
                {
                    "external_variant_id": variation_id,
                    "sku": (
                        str(variation.get("seller_custom_field") or "").strip()
                        or str(variation.get("seller_sku") or "").strip()
                        or f"{base_sku}-{variation_id or 'default'}"
                    ),
                    "title": title,
                    "quantity": max(self._as_int(variation.get("available_quantity"), 0), 0),
                    "option_values": option_values,
                    "images": self._picture_urls_of(variation),
                    "price": str(self._as_decimal(variation.get("price"))),
                }
            )
        if not variants:
            variants.append(
                {
                    "external_variant_id": None,
                    "sku": base_sku,
                    "title": title,
                    "quantity": max(self._as_int(body.get("available_quantity"), 0), 0),
                    "option_values": {},
                    "images": pictures,
                    "price": str(self._as_decimal(body.get("price"))),
                }
            )
        return NormalizedProduct(external_product_id=item_id, title=title, variants=variants)

    @staticmethod
    def _picture_urls_of(payload: Mapping[str, Any]) -> list[str]:
        urls: list[str] = []
        for picture in _sequence(payload.get("pictures")):
            if not isinstance(picture, Mapping):
                continue
            url = str(picture.get("secure_url") or picture.get("url") or "").strip()
            if url and url not in urls:
                urls.append(url)
        return urls

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Inform the Mercado Libre shipment of the carrier and tracking number.

        ``fulfillment_order_id`` is the shipment id when the caller already resolved it;
        otherwise the shipment is resolved from the order's ``shipping.id``.
        """
        order_id = str(external_order_id or "").strip()
        if not order_id:
            raise AppError(
                slug="mercado-libre-order-id-required",
                status_code=422,
                code="ERR_MERCADO_LIBRE_ORDER_ID_REQUIRED",
                title="Mercado Libre Order ID Required",
                detail="A Mercado Libre fulfillment needs the external order id.",
            )
        tracking = str(tracking_number or "").strip()
        if not tracking:
            raise AppError(
                slug="mercado-libre-tracking-number-required",
                status_code=422,
                code="ERR_MERCADO_LIBRE_TRACKING_NUMBER_REQUIRED",
                title="Mercado Libre Tracking Number Required",
                detail="Mercado Libre refuses a shipment update without a tracking number.",
            )
        if self.fulfillment_mode == "full":
            raise AppError(
                slug="mercado-libre-full-fulfillment-tracking-unsupported",
                status_code=422,
                code="ERR_MERCADO_LIBRE_FULL_FULFILLMENT_TRACKING_UNSUPPORTED",
                title="Mercado Libre Full Fulfillment Tracking Unsupported",
                detail=(
                    "Mercado Envíos Full shipments are dispatched by Mercado Libre itself, so "
                    "the seller cannot push a carrier or tracking number for order "
                    f"'{order_id}'."
                ),
            )

        shipment_id = str(fulfillment_order_id or "").strip()
        if not shipment_id:
            order_response = await self._request(
                "GET", f"{MERCADO_LIBRE_ORDERS_PATH}/{quote(order_id, safe='')}"
            )
            if order_response.status_code >= 400:
                self._raise_api_error(
                    order_response,
                    slug="mercado-libre-fulfillment-rejected",
                    code="ERR_MERCADO_LIBRE_FULFILLMENT_REJECTED",
                    title="Mercado Libre Fulfillment Rejected",
                )
            shipment_id = (
                str(_mapping(self._json_object(order_response).get("shipping")).get("id") or "")
                .strip()
            )
            if not shipment_id:
                raise AppError(
                    slug="mercado-libre-shipment-not-found",
                    status_code=502,
                    code="ERR_MERCADO_LIBRE_SHIPMENT_NOT_FOUND",
                    title="Mercado Libre Shipment Not Found",
                    detail=(
                        f"Order '{order_id}' carries no Mercado Libre shipment id, so no "
                        "tracking number can be attached."
                    ),
                )

        body: dict[str, Any] = {
            "tracking_number": tracking,
            "tracking_method": str(carrier_company or "").strip() or None,
            "status": "shipped",
        }
        if tracking_url:
            body["tracking_url"] = str(tracking_url).strip()
        response = await self._request(
            "PUT",
            f"{MERCADO_LIBRE_SHIPMENTS_PATH}/{quote(shipment_id, safe='')}",
            json_body=body,
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="mercado-libre-fulfillment-rejected",
                code="ERR_MERCADO_LIBRE_FULFILLMENT_REJECTED",
                title="Mercado Libre Fulfillment Rejected",
            )
        logger.info(
            "mercado_libre_fulfillment_pushed",
            seller_id=self.seller_id,
            site_id=self.site_id,
            external_order_id=order_id,
            external_shipment_id=shipment_id,
            notify_customer=notify_customer,
        )
        return {
            "channel": "mercado_libre",
            "external_order_id": order_id,
            "external_shipment_id": shipment_id,
            "tracking_number": tracking,
            "carrier": str(carrier_company or "").strip() or None,
            "notify_customer": bool(notify_customer),
            "raw": self._json_object(response),
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_mercado_libre_webhook(raw_body, headers, secret)

    # ---------------------------------------------------------------------- item publishing

    @staticmethod
    def _resolve_category_id(raw: str) -> str:
        """Accept a real Mercado Libre leaf category id; map a catalog label to the default."""
        candidate = str(raw or "").strip().upper()
        return candidate if _CATEGORY_ID_PATTERN.match(candidate) else (
            MERCADO_LIBRE_DEFAULT_CATEGORY_ID
        )

    @staticmethod
    def _picture_urls(payload: MarketplaceListingPayload) -> list[str]:
        urls: list[str] = []
        candidates = [*payload.images, *(variant.image_url for variant in payload.variants)]
        for candidate in candidates:
            url = str(candidate or "").strip()
            if url.startswith("https://") and url not in urls:
                urls.append(url)
        return urls[:MERCADO_LIBRE_MAX_PICTURES]

    @staticmethod
    def _compose_description(payload: MarketplaceListingPayload) -> str:
        blocks = [payload.description.strip()]
        bullets = [str(point).strip() for point in payload.bullet_points if str(point).strip()]
        if bullets:
            blocks.append("\n".join(f"- {point}" for point in bullets))
        details: list[str] = []
        category = str(payload.category or "").strip()
        if category:
            details.append(f"- Category: {category}")
        if payload.hs_code:
            details.append(f"- HS code: {payload.hs_code}")
        for tier in payload.moq_tiers:
            unit_price = Decimal(tier.unit_price_usd).quantize(
                MONEY_SCALE, rounding=ROUND_HALF_UP
            )
            details.append(f"- MOQ {tier.min_qty}: {unit_price} USD per unit")
        if details:
            blocks.append("\n".join(details))
        return "\n\n".join(block for block in blocks if block)

    @staticmethod
    def _variation_bodies(payload: MarketplaceListingPayload) -> list[dict[str, Any]]:
        """Map catalog variants onto Mercado Libre ``variations`` (ordered by options)."""
        option_order = [
            str(option.name).strip() for option in payload.options if str(option.name).strip()
        ]
        bodies: list[dict[str, Any]] = []
        for variant in payload.variants[:MERCADO_LIBRE_MAX_VARIATIONS]:
            values_by_option = {
                str(name).strip(): str(value).strip()
                for name, value in variant.option_values.items()
                if str(name).strip() and str(value).strip()
            }
            ordered = [
                (name, values_by_option[name])
                for name in option_order
                if name in values_by_option
            ]
            ordered.extend(
                sorted(
                    (name, value)
                    for name, value in values_by_option.items()
                    if name not in option_order
                )
            )
            combos = [
                {"name": name, "value_name": value}
                for name, value in ordered[:MERCADO_LIBRE_MAX_VARIATION_ATTRIBUTES]
            ]
            entry: dict[str, Any] = {
                "seller_custom_field": str(variant.external_sku).strip()
                or payload.external_sku.strip(),
                "available_quantity": max(0, int(variant.quantity)),
                "price": str(
                    Decimal(variant.price_usd).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
                ),
            }
            if combos:
                entry["attribute_combinations"] = combos
            bodies.append(entry)
        return bodies

    async def _upload_description(self, item_id: str, plain_text: str) -> bool:
        """Attach the long description; a rejection is logged, not silently swallowed."""
        if not plain_text.strip():
            return False
        response = await self._request(
            "PUT",
            f"{MERCADO_LIBRE_ITEMS_PATH}/{quote(item_id, safe='')}/description",
            json_body={"plain_text": plain_text},
        )
        uploaded = response.status_code < 300
        if not uploaded:
            logger.warning(
                "mercado_libre_description_upload_failed",
                seller_id=self.seller_id,
                site_id=self.site_id,
                external_product_id=item_id,
                status_code=response.status_code,
            )
        return uploaded

    @staticmethod
    def _variant_receipts(
        item: Mapping[str, Any],
        payload: MarketplaceListingPayload,
        *,
        fallback_sku: str,
    ) -> list[MarketplaceVariantReceipt]:
        item_id = str(item.get("id") or "").strip() or None
        receipts: list[MarketplaceVariantReceipt] = []
        for row in _sequence(item.get("variations")):
            if not isinstance(row, Mapping):
                continue
            variation_id = str(row.get("id") or "").strip() or None
            receipts.append(
                MarketplaceVariantReceipt(
                    external_sku=str(row.get("seller_custom_field") or "").strip()
                    or fallback_sku,
                    external_variant_id=variation_id,
                    external_inventory_item_id=variation_id,
                )
            )
        if not receipts:
            listed_variant = payload.variants[0].external_sku.strip() if payload.variants else ""
            receipts.append(
                MarketplaceVariantReceipt(
                    external_sku=listed_variant or fallback_sku,
                    external_variant_id=None,
                    external_inventory_item_id=item_id,
                )
            )
        return receipts

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create one Mercado Libre item (single or multi-variation) plus its description."""
        sku = str(payload.external_sku).strip()
        title = str(payload.title).strip()[:MERCADO_LIBRE_MAX_TITLE_LENGTH]
        if not sku or not title:
            raise AppError(
                slug="mercado-libre-listing-invalid",
                status_code=422,
                code="ERR_MERCADO_LIBRE_LISTING_INVALID",
                title="Mercado Libre Listing Invalid",
                detail="A Mercado Libre listing requires a non-empty SKU and title.",
            )
        if isinstance(payload.price_usd, float):
            raise AppError(
                slug="mercado-libre-invalid-money",
                status_code=422,
                code="ERR_MERCADO_LIBRE_INVALID_MONEY",
                title="Invalid Mercado Libre Money Value",
                detail=(
                    "Mercado Libre listing prices must be Decimal; IEEE-754 float prices are "
                    "refused so no binary rounding error can reach a live listing."
                ),
            )
        price = Decimal(payload.price_usd).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
        if price <= Decimal("0"):
            raise AppError(
                slug="mercado-libre-listing-invalid",
                status_code=422,
                code="ERR_MERCADO_LIBRE_LISTING_INVALID",
                title="Mercado Libre Listing Invalid",
                detail="A Mercado Libre listing price must be greater than zero.",
            )
        pictures = self._picture_urls(payload)
        if not pictures and payload.image_blobs:
            raise AppError(
                slug="mercado-libre-image-blob-unsupported",
                status_code=422,
                code="ERR_MERCADO_LIBRE_IMAGE_BLOB_UNSUPPORTED",
                title="Mercado Libre Image Blob Unsupported",
                detail=(
                    "Mercado Libre items only accept HTTPS picture URLs; raw image blobs "
                    "cannot be uploaded through the items API."
                ),
            )

        body: dict[str, Any] = {
            "title": title,
            "category_id": self._resolve_category_id(payload.category),
            "price": str(price),
            "currency_id": self.currency_id,
            "available_quantity": max(0, self._as_int(payload.quantity, 0)),
            "condition": "new",
            "listing_type_id": MERCADO_LIBRE_LISTING_TYPE_ID,
            "buying_mode": "buy_it_now",
            "pictures": [{"source": url} for url in pictures],
        }
        variations = self._variation_bodies(payload)
        if variations:
            body["variations"] = variations

        response = await self._request("POST", MERCADO_LIBRE_ITEMS_PATH, json_body=body)
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="mercado-libre-listing-rejected",
                code="ERR_MERCADO_LIBRE_LISTING_REJECTED",
                title="Mercado Libre Listing Rejected",
            )
        item = self._json_object(response)
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise AppError(
                slug="mercado-libre-listing-rejected",
                status_code=502,
                code="ERR_MERCADO_LIBRE_LISTING_REJECTED",
                title="Mercado Libre Listing Rejected",
                detail=(
                    "Mercado Libre accepted the item request but returned no item id for "
                    f"SKU '{sku}'."
                ),
            )
        description_uploaded = await self._upload_description(
            item_id, self._compose_description(payload)
        )
        variant_receipts = self._variant_receipts(item, payload, fallback_sku=sku)
        logger.info(
            "mercado_libre_listing_published",
            seller_id=self.seller_id,
            site_id=self.site_id,
            sku=sku,
            external_product_id=item_id,
            variations=len(variant_receipts),
            description_uploaded=description_uploaded,
        )
        raw_response = dict(item)
        raw_response["description_uploaded"] = description_uploaded
        return MarketplaceListingDispatchReceipt(
            external_product_id=item_id,
            external_sku=sku,
            status=str(item.get("status") or "PUBLISHED").strip() or "PUBLISHED",
            variant_receipts=variant_receipts,
            raw_response=raw_response,
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
        """Push one SKU's sellable quantity onto the item or one of its variations.

        Mercado Libre's items API holds a single ``available_quantity`` per item/variation,
        so ``location_id``/``warehouse_id`` have no addressable dimension here and extra
        outbox fields are accepted purely for the shared dispatcher signature.
        """
        del location_id, warehouse_id
        clean_sku = str(external_sku or "").strip()
        if not clean_sku:
            raise AppError(
                slug="mercado-libre-inventory-sync-failed",
                status_code=422,
                code="ERR_MERCADO_LIBRE_INVENTORY_SYNC",
                title="Mercado Libre Inventory Sync Failed",
                detail="Mercado Libre inventory sync requires a non-empty external_sku.",
            )
        item_id = str(external_product_id or "").strip()
        if not item_id:
            raise AppError(
                slug="mercado-libre-inventory-sync-failed",
                status_code=422,
                code="ERR_MERCADO_LIBRE_INVENTORY_SYNC",
                title="Mercado Libre Inventory Sync Failed",
                detail=(
                    f"Cannot push stock for SKU '{clean_sku}' without the Mercado Libre item "
                    "id (external_product_id)."
                ),
            )
        variation_id = str(external_variant_id or "").strip()
        inventory_id = str(external_inventory_item_id or "").strip()
        if not variation_id and inventory_id and inventory_id != item_id:
            # ``publish_listing`` records the variation id as the inventory item id.
            variation_id = inventory_id
        path = (
            f"{MERCADO_LIBRE_ITEMS_PATH}/{quote(item_id, safe='')}"
            f"/variations/{quote(variation_id, safe='')}"
            if variation_id
            else f"{MERCADO_LIBRE_ITEMS_PATH}/{quote(item_id, safe='')}"
        )
        norm_qty = max(0, int(quantity))
        response = await self._request(
            "PUT", path, json_body={"available_quantity": norm_qty}
        )
        if response.status_code >= 300:
            self._raise_api_error(
                response,
                slug="mercado-libre-inventory-sync-failed",
                code="ERR_MERCADO_LIBRE_INVENTORY_SYNC",
                title="Mercado Libre Inventory Sync Failed",
            )
        return {
            "channel": "mercado_libre",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_product_id": item_id,
            "external_variant_id": variation_id or None,
            "site_id": self.site_id,
            "currency_id": self.currency_id,
            "raw": self._json_object(response),
        }
