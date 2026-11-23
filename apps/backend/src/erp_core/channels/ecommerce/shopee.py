"""Shopee Open Platform v2 connector: signed API calls, push verification, live publish.

Transport contract
------------------
Shopee signs *every* call with ``HMAC-SHA256(partner_key, f"{partner_id}{path}{ts}...")``.
All request parameters travel in the query string (including for ``POST``), so the
signature covers the path, the partner id, the unix timestamp, the access token and the
shop id — never the JSON body.

Security
--------
``partner_key`` and ``access_token`` are held privately, never appear in ``repr``/``str``,
log records or ``AppError`` details. Push callbacks are verified with a constant-time
HMAC-SHA256 comparison on ``x-shopee-hmac-sha256`` (legacy pushes: ``Authorization``).

Money
-----
Every amount is a :class:`~decimal.Decimal` quantized to 2 decimals with
``ROUND_HALF_UP``; no monetary value ever passes through ``float``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
import structlog

from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
    MarketplaceListingDispatchReceipt,
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
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

SHOPEE_API_BASE = "https://partner.shopeemobile.com"
SHOPEE_API_PREFIX = "/api/v2"

ORDER_LIST_PATH = f"{SHOPEE_API_PREFIX}/order/get_order_list"
ORDER_DETAIL_PATH = f"{SHOPEE_API_PREFIX}/order/get_order_detail"
ITEM_LIST_PATH = f"{SHOPEE_API_PREFIX}/product/get_item_list"
ITEM_BASE_INFO_PATH = f"{SHOPEE_API_PREFIX}/product/get_item_base_info"
MODEL_LIST_PATH = f"{SHOPEE_API_PREFIX}/product/get_model_list"
ADD_ITEM_PATH = f"{SHOPEE_API_PREFIX}/product/add_item"
UPDATE_STOCK_PATH = f"{SHOPEE_API_PREFIX}/product/update_stock"
SHIP_ORDER_PATH = f"{SHOPEE_API_PREFIX}/logistics/ship_order"
LOGISTICS_CHANNEL_LIST_PATH = f"{SHOPEE_API_PREFIX}/logistics/get_channel_list"
UPLOAD_IMAGE_PATH = f"{SHOPEE_API_PREFIX}/media_space/upload_image"

#: Listing prices are pushed in the currency the catalog prices them in (USD). The ERP
#: never guesses a local selling currency; an FX re-pricing belongs to the merchant.
SHOPEE_LISTING_CURRENCY = "USD"

MONEY_SCALE = Decimal("0.01")
_PAGE_SIZE = 50
_MAX_IMAGES = 9
_MAX_LOGISTIC_CHANNELS = 5
_DEFAULT_PACKAGE_WEIGHT_KG = "0.5"
_DEFAULT_PACKAGE_DIMENSION_CM: Mapping[str, int] = {
    "package_length": 10,
    "package_width": 10,
    "package_height": 10,
}

#: Shipment shapes this connector can honestly put on the wire. ``dropoff``/``pickup``
#: integrated shipping needs the shop's branch id / pickup window, which is merchant
#: configuration this connector does not manage, so those modes fail closed instead of
#: inventing a window.
_FULFILLMENT_MODES = frozenset({"standard", "dropoff", "pickup"})

_SHOPEE_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "UNPAID": FulfillmentStatus.PENDING_PAYMENT,
    "READY_TO_SHIP": FulfillmentStatus.CONFIRMED_RESERVED,
    "PROCESSED": FulfillmentStatus.CONFIRMED_RESERVED,
    "RETRY_SHIP": FulfillmentStatus.CONFIRMED_RESERVED,
    "IN_CANCEL": FulfillmentStatus.CONFIRMED_RESERVED,
    "TO_RETURN": FulfillmentStatus.EXCEPTION,
    "SHIPPED": FulfillmentStatus.SHIPPED,
    "TO_CONFIRM_RECEIVE": FulfillmentStatus.SHIPPED,
    "COMPLETED": FulfillmentStatus.DELIVERED,
    "CANCELLED": FulfillmentStatus.CANCELLED,
}


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _money_str(value: Decimal) -> str:
    """Serialize money for Shopee: fixed 2dp, half-up, never scientific notation."""
    return str(_money(value))


def _header_value(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (HTTP header names are not case sensitive)."""
    direct = headers.get(name)
    if direct:
        return str(direct)
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered and value:
            return str(value)
    return ""


def sign_shopee_request(
    *,
    partner_id: str | int,
    partner_key: str,
    api_path: str,
    timestamp: int,
    access_token: str = "",
    shop_id: str | int = "",
) -> str:
    """Shopee v2 signature: HMAC-SHA256(partner_key, partner_id+path+ts+token+shop_id)."""
    if not partner_key:
        raise ValueError("partner_key is required to sign Shopee Open Platform requests")
    base_string = f"{partner_id}{api_path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base_string.encode(), hashlib.sha256).hexdigest()


def verify_shopee_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    partner_key: str,
    *,
    callback_url: str | None = None,
) -> bool:
    """Constant-time verification of a Shopee push callback (fail-closed).

    Shopee signs ``f"{callback_url}|{raw_body}"`` with the partner key and sends the hex
    digest in ``x-shopee-hmac-sha256`` (legacy pushes put it in ``Authorization``). Pushes
    signed over the raw body only are accepted too, because partners that do not register
    a callback url see that shape.
    """
    received = (
        _header_value(headers, "x-shopee-hmac-sha256") or _header_value(headers, "authorization")
    ).strip().lower()
    if not received or not partner_key:
        return False
    key = partner_key.encode()
    candidates = [hmac.new(key, raw_body, hashlib.sha256).hexdigest()]
    if callback_url is not None and callback_url.strip():
        signed = callback_url.strip().encode() + b"|" + raw_body
        candidates.append(hmac.new(key, signed, hashlib.sha256).hexdigest())
    return any(hmac.compare_digest(candidate, received) for candidate in candidates)


class ShopeeAdapter(CommercePlatformAdapter):
    """Shopee Open Platform (v2) connector for one shop/region.

    Credentials and the timeout are private; only ``shop_id``, ``partner_id``, ``region``,
    ``fulfillment_mode`` and ``endpoint_url`` are safe to echo back.
    """

    def __init__(
        self,
        *,
        shop_id: str,
        partner_id: str | int,
        partner_key: str,
        access_token: str,
        region: str = "GLOBAL",
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        clean_shop_id = str(shop_id).strip()
        clean_partner_id = str(partner_id).strip()
        clean_partner_key = (partner_key or "").strip()
        clean_access_token = (access_token or "").strip()
        if not (clean_shop_id and clean_partner_id and clean_partner_key and clean_access_token):
            raise AppError(
                slug="shopee-credentials-missing",
                status_code=422,
                code="ERR_SHOPEE_CREDENTIALS_MISSING",
                title="Shopee Credentials Missing",
                detail=(
                    "Shopee requires a non-empty shop_id, partner_id, partner_key and "
                    "access_token."
                ),
            )
        mode = (fulfillment_mode or "").strip().lower()
        if mode not in _FULFILLMENT_MODES:
            raise AppError(
                slug="shopee-fulfillment-mode-invalid",
                status_code=422,
                code="ERR_SHOPEE_FULFILLMENT_MODE_INVALID",
                title="Shopee Fulfillment Mode Invalid",
                detail=(
                    f"Unknown Shopee fulfillment_mode '{fulfillment_mode}'; expected one of "
                    f"{sorted(_FULFILLMENT_MODES)}."
                ),
            )
        self.shop_id = clean_shop_id
        self.partner_id = clean_partner_id
        #: Shopee serves every marketplace from one partner host; the region only labels
        #: which shop marketplace (SG/MY/ID/TH/VN/PH/TW/BR/MX) the account belongs to.
        self.region = (region or "GLOBAL").strip().upper() or "GLOBAL"
        self.fulfillment_mode = mode
        self.base_url = (endpoint_url or SHOPEE_API_BASE).rstrip("/")
        self._partner_key = clean_partner_key
        self._access_token = clean_access_token
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._logistic_info_cache: list[dict[str, Any]] | None = None

    def __repr__(self) -> str:
        return (
            f"ShopeeAdapter(shop_id={self.shop_id!r}, partner_id={self.partner_id!r}, "
            f"region={self.region!r}, fulfillment_mode={self.fulfillment_mode!r}, "
            f"endpoint_url={self.base_url!r})"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout_seconds, trust_env=False
        )

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a log line or an ``AppError`` detail."""
        return tuple(value for value in (self._partner_key, self._access_token) if value)

    def _redacted(self, text: str, limit: int = 400) -> str:
        cleaned = text[:limit]
        for secret in self._secrets():
            cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned

    def _signed_params(
        self,
        api_path: str,
        params: Mapping[str, str] | None,
        timestamp: int,
    ) -> dict[str, str]:
        signed = {
            "partner_id": self.partner_id,
            "timestamp": str(timestamp),
            "access_token": self._access_token,
            "shop_id": self.shop_id,
            **dict(params or {}),
        }
        signed["sign"] = sign_shopee_request(
            partner_id=self.partner_id,
            partner_key=self._partner_key,
            api_path=api_path,
            timestamp=timestamp,
            access_token=self._access_token,
            shop_id=self.shop_id,
        )
        return signed

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        error_slug: str,
        error_code: str,
        error_title: str,
    ) -> dict[str, Any]:
        status = response.status_code
        try:
            raw: Any = response.json()
        except ValueError:
            raw = None
        body: dict[str, Any] = raw if isinstance(raw, dict) else {}
        error = str(body.get("error") or "").strip()
        message = str(body.get("message") or "").strip()
        request_id = str(body.get("request_id") or "").strip()
        if status in (401, 403):
            raise AppError(
                slug="shopee-auth-failed",
                status_code=502,
                code="ERR_SHOPEE_AUTH_FAILED",
                title="Shopee Authentication Failed",
                detail=f"Shopee rejected the partner credentials or access token (HTTP {status}).",
            )
        if status == 429:
            raise AppError(
                slug="shopee-rate-limited",
                status_code=429,
                code="ERR_SHOPEE_RATE_LIMITED",
                title="Shopee Rate Limited",
                detail="Shopee returned HTTP 429 for the Open Platform call; retry after backoff.",
            )
        if status >= 400 or error:
            summary = f"HTTP {status} {error or 'error'}"
            if message:
                summary += f": {message}"
            if request_id:
                summary += f" (request_id={request_id})"
            raise AppError(
                slug=error_slug,
                status_code=502,
                code=error_code,
                title=error_title,
                detail=self._redacted(summary),
            )
        data = body.get("response")
        return data if isinstance(data, dict) else {}

    async def _request(
        self,
        method: str,
        api_path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        error_slug: str = "shopee-api-error",
        error_code: str = "ERR_SHOPEE_API",
        error_title: str = "Shopee Open Platform Error",
    ) -> dict[str, Any]:
        timestamp = int(time.time())
        body_text = (
            json.dumps(json_body, separators=(",", ":")) if json_body is not None else None
        )
        headers = {"Content-Type": "application/json"} if body_text is not None else {}
        async with self._client() as client:
            response = await client.request(
                method,
                f"{self.base_url}{api_path}",
                params=self._signed_params(api_path, params, timestamp),
                content=body_text,
                headers=headers,
            )
        return self._parse_response(
            response,
            error_slug=error_slug,
            error_code=error_code,
            error_title=error_title,
        )

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value))

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------------ order sync

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Shopee's ``model_discounted_price`` is the *unit* price: multiply by quantity."""
        sku = str(item.get("model_sku") or item.get("item_sku") or "").strip()
        if not sku:
            return None
        quantity = max(1, self._int(item.get("model_quantity_purchased"), 1))
        unit_price = self._decimal(
            item.get("model_discounted_price") or item.get("model_original_price")
        )
        explicit_total = item.get("line_total") or item.get("model_total_price")
        line_total = (
            self._decimal(explicit_total)
            if explicit_total not in (None, "")
            else unit_price * Decimal(quantity)
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(item.get("item_name") or sku),
            quantity=quantity,
            line_total=_money(line_total),
        )

    @staticmethod
    def _payment_status(order_status: str, refunded: Decimal, total: Decimal) -> PaymentStatus:
        """Only a full refund (or a cancelled order) flips the payment to ``refunded``."""
        if order_status == "UNPAID":
            return PaymentStatus.UNPAID
        if order_status == "CANCELLED":
            return PaymentStatus.REFUNDED
        if total > Decimal("0") and refunded >= total:
            return PaymentStatus.REFUNDED
        return PaymentStatus.PAID

    def _normalize_order(self, order: Mapping[str, Any]) -> NormalizedOrder:
        status = str(order.get("order_status") or order.get("status") or "").strip().upper()
        recipient_raw = order.get("recipient_address")
        recipient: Mapping[str, Any] = recipient_raw if isinstance(recipient_raw, Mapping) else {}
        line_items = [
            line
            for item in order.get("item_list") or []
            if isinstance(item, Mapping) and (line := self._normalize_line_item(item)) is not None
        ]
        total_amount = _money(self._decimal(order.get("total_amount")))
        subtotal = (
            sum((line.line_total for line in line_items), Decimal("0.00"))
            if line_items
            else total_amount
        )
        refunded = self._decimal(order.get("refunded_amount") or order.get("refund_amount"))
        commission = self._decimal(
            order.get("commission_fee") or order.get("platform_commission")
        )
        email = str(order.get("buyer_email") or "").strip() or None
        phone = str(recipient.get("phone") or "").strip() or None
        buyer_username = str(order.get("buyer_username") or "").strip() or None
        country = str(recipient.get("region") or recipient.get("country") or "").strip().upper()
        metadata: dict[str, Any] = {
            "shopee_order_status": status,
            "shopee_order_sn": str(order.get("order_sn") or order.get("order_id") or ""),
            "shopee_region": self.region,
        }
        if buyer_username:
            metadata["shopee_buyer_username"] = buyer_username
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )
        return NormalizedOrder(
            external_order_id=str(order.get("order_sn") or order.get("order_id") or ""),
            external_order_number=str(order.get("order_sn") or order.get("order_id") or "") or None,
            order_mode=OrderMode.B2C_RETAIL,
            currency=str(order.get("currency") or "USD").strip().upper(),
            subtotal_amount=_money(subtotal),
            shipping_charged=_money(self._decimal(order.get("estimated_shipping_fee"))),
            discount_amount=_money(self._decimal(order.get("seller_discount"))),
            tax_collected=_money(self._decimal(order.get("tax_amount"))),
            total_amount=total_amount,
            platform_commission_base=_money(commission),
            fulfillment_status=_SHOPEE_STATUS_MAP.get(
                status, FulfillmentStatus.CONFIRMED_RESERVED
            ),
            payment_status=self._payment_status(status, refunded, total_amount),
            customer=NormalizedCustomer(
                email=email,
                phone_e164=phone,
                external_customer_id=buyer_username,
                external_identity_type=(
                    IdentityType.EMAIL if email else IdentityType.WEB_VISITOR_ID
                ),
                display_name=str(
                    recipient.get("name") or buyer_username or email or "Shopee Buyer"
                ),
                country_code=(country[:2] or None) if country else None,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=datetime.fromtimestamp(
                self._int(order.get("update_time"), 0), UTC
            ),
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        """List order SNs updated after ``since``, then hydrate their full detail."""
        search = await self._request(
            "GET",
            ORDER_LIST_PATH,
            params={
                "time_range_field": "update_time",
                "time_from": str(int(since.timestamp())),
                "time_to": str(int(time.time())),
                "page_size": str(_PAGE_SIZE),
                "response_optional_fields": "order_status",
                **({"cursor": cursor} if cursor else {}),
            },
        )
        order_sns = [
            str(row["order_sn"])
            for row in search.get("order_list") or []
            if isinstance(row, Mapping) and row.get("order_sn")
        ]
        next_cursor = str(search.get("next_cursor") or "").strip() or None
        has_more = bool(search.get("more")) if "more" in search else bool(next_cursor)
        if not order_sns:
            return [], next_cursor if has_more else None
        details = await self._request(
            "GET",
            ORDER_DETAIL_PATH,
            params={
                "order_sn_list": ",".join(order_sns),
                "response_optional_fields": (
                    "buyer_username,buyer_email,item_list,recipient_address,package_list"
                ),
            },
        )
        orders = [
            self._normalize_order(row)
            for row in details.get("order_list") or []
            if isinstance(row, Mapping) and (row.get("item_list") or [])
        ]
        return orders, next_cursor if has_more else None

    # ---------------------------------------------------------------------- product sync

    def _normalize_models(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        tier_names: list[str] = []
        option_lists: list[list[str]] = []
        for tier in data.get("tier_variation") or []:
            if not isinstance(tier, Mapping):
                continue
            tier_names.append(str(tier.get("name") or ""))
            option_lists.append(
                [
                    str(option.get("option") or "")
                    for option in tier.get("option_list") or []
                    if isinstance(option, Mapping)
                ]
            )
        variants: list[dict[str, Any]] = []
        for row in data.get("model") or []:
            if not isinstance(row, Mapping):
                continue
            option_values: dict[str, str] = {}
            tier_index = row.get("tier_index")
            for position, index in enumerate(tier_index if isinstance(tier_index, list) else []):
                if position >= len(option_lists) or not isinstance(index, int):
                    continue
                options = option_lists[position]
                if 0 <= index < len(options):
                    option_values[tier_names[position] or f"tier_{position}"] = options[index]
            variants.append(
                {
                    "external_variant_id": (
                        str(row["model_id"]) if row.get("model_id") is not None else None
                    ),
                    "external_sku": str(row.get("model_sku") or ""),
                    "price": _money_str(self._decimal(self._model_price_value(row))),
                    "quantity": self._model_stock(row),
                    "option_values": option_values,
                }
            )
        return variants

    @staticmethod
    def _model_price_value(row: Mapping[str, Any]) -> Any:
        price_info = row.get("price_info")
        if isinstance(price_info, list) and price_info and isinstance(price_info[0], Mapping):
            first = price_info[0]
            return first.get("current_price") or first.get("original_price")
        return row.get("price")

    def _model_stock(self, row: Mapping[str, Any]) -> int:
        stock_info = row.get("stock_info_v2")
        if isinstance(stock_info, Mapping):
            seller_stock = stock_info.get("seller_stock")
            if isinstance(seller_stock, list) and seller_stock:
                first = seller_stock[0]
                if isinstance(first, Mapping):
                    return max(0, self._int(first.get("stock"), 0))
        return max(0, self._int(row.get("stock"), 0))

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        """Page through items updated after ``since``; hydrate base info + model list."""
        offset = max(0, self._int(cursor, 0))
        listing = await self._request(
            "GET",
            ITEM_LIST_PATH,
            params={
                "offset": str(offset),
                "page_size": str(_PAGE_SIZE),
                "item_status": "NORMAL",
                "update_time_from": str(int(since.timestamp())),
                "update_time_to": str(int(time.time())),
            },
        )
        item_ids = [
            str(row["item_id"])
            for row in listing.get("item") or []
            if isinstance(row, Mapping) and row.get("item_id") is not None
        ]
        next_offset = listing.get("next_offset")
        next_cursor = (
            str(next_offset)
            if listing.get("has_next_page") and next_offset is not None
            else None
        )
        if not item_ids:
            return [], next_cursor
        base = await self._request(
            "GET", ITEM_BASE_INFO_PATH, params={"item_id_list": ",".join(item_ids)}
        )
        products: list[NormalizedProduct] = []
        for item in base.get("item_list") or []:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("item_id") or "").strip()
            if not item_id:
                continue
            models = await self._request("GET", MODEL_LIST_PATH, params={"item_id": item_id})
            products.append(
                NormalizedProduct(
                    external_product_id=item_id,
                    title=str(item.get("item_name") or item_id),
                    variants=self._normalize_models(models),
                )
            )
        return products, next_cursor

    # ------------------------------------------------------------------------ fulfillment

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Ship one order through ``/logistics/ship_order`` (non-integrated tracking)."""
        order_sn = (external_order_id or "").strip()
        tracking = (tracking_number or "").strip()
        carrier = (carrier_company or "").strip()
        if not order_sn or not tracking or not carrier:
            raise AppError(
                slug="shopee-fulfillment-rejected",
                status_code=422,
                code="ERR_SHOPEE_FULFILLMENT_REJECTED",
                title="Shopee Fulfillment Rejected",
                detail=(
                    "Shopee ship_order requires a non-empty order_sn, tracking_number and "
                    "shipping_carrier."
                ),
            )
        if self.fulfillment_mode != "standard":
            raise AppError(
                slug="shopee-fulfillment-mode-unsupported",
                status_code=422,
                code="ERR_SHOPEE_FULFILLMENT_MODE_UNSUPPORTED",
                title="Shopee Fulfillment Mode Unsupported",
                detail=(
                    f"fulfillment_mode '{self.fulfillment_mode}' ships an already-booked Shopee "
                    "package and needs the shop's branch id / pickup window, which this "
                    "connector does not manage; use 'standard' non-integrated shipping."
                ),
            )
        body: dict[str, Any] = {
            "order_sn": order_sn,
            "tracking_number": tracking,
            "shipping_carrier": carrier,
        }
        package_number = (fulfillment_order_id or "").strip()
        if package_number:
            body["package_number"] = package_number
        data = await self._request(
            "POST",
            SHIP_ORDER_PATH,
            json_body=body,
            error_slug="shopee-fulfillment-rejected",
            error_code="ERR_SHOPEE_FULFILLMENT_REJECTED",
            error_title="Shopee Fulfillment Rejected",
        )
        return {
            "channel": "shopee",
            "external_order_id": order_sn,
            "tracking_number": tracking,
            "carrier_company": carrier,
            "tracking_url": tracking_url,
            "package_number": package_number or None,
            "fulfillment_mode": self.fulfillment_mode,
            "notify_customer": notify_customer,
            "raw": data,
        }

    # -------------------------------------------------------------------------- listings

    @staticmethod
    def _description_text(payload: MarketplaceListingPayload) -> str:
        lines = [
            payload.description.strip(),
            *(f"• {point.strip()}" for point in payload.bullet_points if point.strip()),
        ]
        text = "\n".join(line for line in lines if line).strip()
        return (text or payload.title.strip())[:5000]

    @staticmethod
    def _category_id(category: str) -> int:
        digits = "".join(char for char in category if char.isdigit())
        if not digits:
            raise AppError(
                slug="shopee-category-required",
                status_code=422,
                code="ERR_SHOPEE_CATEGORY_REQUIRED",
                title="Shopee Category Required",
                detail=(
                    f"Shopee listings require a numeric category_id; got '{category}'. Map the "
                    "catalog category to a Shopee category id before publishing."
                ),
            )
        return int(digits)

    @staticmethod
    def _option_tiers(
        payload: MarketplaceListingPayload,
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Resolve the listing's tiers from declared options plus observed variant values."""
        names: list[str] = []
        values: dict[str, list[str]] = {}

        def add(name: str, value: str | None = None) -> None:
            key = name.strip()
            if not key:
                return
            if key not in values:
                values[key] = []
                names.append(key)
            if value is None:
                return
            cleaned = value.strip()
            if cleaned and cleaned not in values[key]:
                values[key].append(cleaned)

        for option in payload.options:
            add(option.name)
            for value in option.values:
                add(option.name, value)
        for variant in payload.variants:
            for name, value in variant.option_values.items():
                add(name, value)
        return names, values

    def _model_payload(
        self,
        variant: MarketplaceVariantPayload,
        tiers: list[str],
        tier_values: Mapping[str, list[str]],
    ) -> dict[str, Any]:
        model_sku = (variant.external_sku or "").strip()
        if not model_sku:
            raise AppError(
                slug="shopee-listing-rejected",
                status_code=422,
                code="ERR_SHOPEE_LISTING_REJECTED",
                title="Shopee Listing Rejected",
                detail="Shopee requires a non-empty model_sku for every variant.",
            )
        tier_index: list[int] = []
        for name in tiers:
            value = (variant.option_values.get(name) or "").strip()
            options = tier_values[name]
            if value not in options:
                raise AppError(
                    slug="shopee-listing-rejected",
                    status_code=422,
                    code="ERR_SHOPEE_LISTING_REJECTED",
                    title="Shopee Listing Rejected",
                    detail=(
                        f"Variant '{model_sku}' has no valid value for tier '{name}' "
                        f"(expected one of {options}, got '{value or ''}')."
                    ),
                )
            tier_index.append(options.index(value))
        return {
            "tier_index": tier_index,
            "model_sku": model_sku,
            "price_info": [
                {
                    "currency": SHOPEE_LISTING_CURRENCY,
                    "original_price": _money_str(variant.price_usd),
                }
            ],
            "stock_info_v2": {
                "seller_stock": [{"stock": max(0, int(variant.quantity))}],
            },
        }

    async def _resolve_logistic_info(self) -> list[dict[str, Any]]:
        """Resolve (and cache) the shop's enabled Shopee logistics channels."""
        if self._logistic_info_cache is not None:
            return self._logistic_info_cache
        data = await self._request("GET", LOGISTICS_CHANNEL_LIST_PATH)
        channels: list[dict[str, Any]] = []
        for row in data.get("logistic_channel_list") or data.get("logistics_channel_list") or []:
            if not isinstance(row, Mapping) or row.get("enabled") is False:
                continue
            raw_id = row.get("logistics_channel_id") or row.get("logistic_id")
            channel_id = self._int(raw_id, 0)
            if channel_id > 0:
                channels.append({"logistic_id": channel_id, "enabled": True})
        channels = channels[:_MAX_LOGISTIC_CHANNELS]
        if not channels:
            raise AppError(
                slug="shopee-logistics-unavailable",
                status_code=502,
                code="ERR_SHOPEE_LOGISTICS_UNAVAILABLE",
                title="Shopee Logistics Unavailable",
                detail=(
                    "Shopee returned no enabled logistics channel for this shop; a listing "
                    "cannot be created without at least one."
                ),
            )
        self._logistic_info_cache = channels
        return channels

    async def _upload_image_blob(self, blob: MarketplaceImageBlob) -> str | None:
        """Push one image blob into Shopee media space and return its ``image_id``."""
        if not blob.data:
            return None
        timestamp = int(time.time())
        params = self._signed_params(UPLOAD_IMAGE_PATH, None, timestamp)
        filename = (blob.filename or "product.jpg").strip() or "product.jpg"
        mime_type = (blob.mime_type or "image/jpeg").strip() or "image/jpeg"
        async with self._client() as client:
            response = await client.post(
                f"{self.base_url}{UPLOAD_IMAGE_PATH}",
                params=params,
                files={"image": (filename, bytes(blob.data), mime_type)},
            )
        data = self._parse_response(
            response,
            error_slug="shopee-media-upload-failed",
            error_code="ERR_SHOPEE_MEDIA_UPLOAD",
            error_title="Shopee Image Upload Failed",
        )
        info = data.get("image_info")
        image_id = str(info.get("image_id") or "") if isinstance(info, Mapping) else ""
        return image_id.strip() or None

    @staticmethod
    def _variant_receipts(
        data: Mapping[str, Any],
        payload: MarketplaceListingPayload,
        fallback_sku: str,
    ) -> list[MarketplaceVariantReceipt]:
        """Map Shopee's returned models back onto the SKUs the ERP asked for."""
        rows = [
            row
            for row in data.get("model") or data.get("model_list") or []
            if isinstance(row, Mapping)
        ]
        by_sku: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            sku = str(row.get("model_sku") or "").strip()
            if sku:
                by_sku[sku] = row
        receipts: list[MarketplaceVariantReceipt] = []
        if payload.variants:
            for variant in payload.variants:
                sku = (variant.external_sku or "").strip() or fallback_sku
                row = by_sku.get(sku) or {}
                model_id = row.get("model_id")
                receipts.append(
                    MarketplaceVariantReceipt(
                        external_sku=sku,
                        external_variant_id=(
                            str(model_id) if model_id is not None else None
                        ),
                    )
                )
            return receipts
        if rows:
            for row in rows:
                sku = str(row.get("model_sku") or "").strip() or fallback_sku
                model_id = row.get("model_id")
                receipts.append(
                    MarketplaceVariantReceipt(
                        external_sku=sku,
                        external_variant_id=(
                            str(model_id) if model_id is not None else None
                        ),
                    )
                )
        return receipts

    async def publish_listing(
        self, payload: MarketplaceListingPayload
    ) -> MarketplaceListingDispatchReceipt:
        """Create one single- or multi-variant Shopee listing via ``/product/add_item``."""
        clean_sku = payload.external_sku.strip()
        clean_title = payload.title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="shopee-listing-rejected",
                status_code=422,
                code="ERR_SHOPEE_LISTING_REJECTED",
                title="Shopee Listing Rejected",
                detail="Shopee listing requires a non-empty SKU and title.",
            )
        image_ids: list[str] = []
        for blob in payload.image_blobs:
            image_id = await self._upload_image_blob(blob)
            if image_id:
                image_ids.append(image_id)
        image_urls = [url.strip() for url in payload.images if url.strip()][:_MAX_IMAGES]

        body: dict[str, Any] = {
            "item_name": clean_title[:255],
            "description": self._description_text(payload),
            "item_status": "NORMAL",
            "category_id": self._category_id(payload.category),
            "weight": _DEFAULT_PACKAGE_WEIGHT_KG,
            "dimension": dict(_DEFAULT_PACKAGE_DIMENSION_CM),
            "item_dangerous": 0,
            "logistic_info": await self._resolve_logistic_info(),
        }
        image_payload: dict[str, Any] = {}
        if image_ids:
            image_payload["image_id_list"] = image_ids[:_MAX_IMAGES]
        if image_urls:
            # Public URLs are forwarded for server-side fetch; media-space ids (from
            # uploaded blobs) are always preferred.
            image_payload["image_url_list"] = image_urls
        if image_payload:
            body["image"] = image_payload

        tiers, tier_values = self._option_tiers(payload)
        if tiers:
            body["tier_variation"] = [
                {
                    "name": name,
                    "option_list": [{"option": value} for value in tier_values[name]],
                }
                for name in tiers
            ]
        if payload.variants:
            body["model"] = [
                self._model_payload(variant, tiers, tier_values)
                for variant in payload.variants
            ]
        else:
            price_info = [
                {
                    "currency": SHOPEE_LISTING_CURRENCY,
                    "original_price": _money_str(payload.price_usd),
                }
            ]
            stock_info = {"seller_stock": [{"stock": max(0, int(payload.quantity))}]}
            body["price_info"] = price_info
            body["stock_info_v2"] = stock_info
            body["model"] = [
                {
                    "tier_index": [],
                    "model_sku": clean_sku,
                    "price_info": price_info,
                    "stock_info_v2": stock_info,
                }
            ]

        data = await self._request(
            "POST",
            ADD_ITEM_PATH,
            json_body=body,
            error_slug="shopee-listing-rejected",
            error_code="ERR_SHOPEE_LISTING_REJECTED",
            error_title="Shopee Listing Rejected",
        )
        item_id = str(data.get("item_id") or "").strip()
        if not item_id:
            raise AppError(
                slug="shopee-listing-rejected",
                status_code=502,
                code="ERR_SHOPEE_LISTING_REJECTED",
                title="Shopee Listing Rejected",
                detail=f"Shopee returned no item_id for SKU '{clean_sku}'.",
            )
        logger.info(
            "shopee_listing_published",
            shop_id=self.shop_id,
            region=self.region,
            external_sku=clean_sku,
            item_id=item_id,
        )
        return MarketplaceListingDispatchReceipt(
            external_product_id=item_id,
            external_sku=clean_sku,
            status=str(data.get("item_status") or "NORMAL"),
            raw_response=data,
            variant_receipts=self._variant_receipts(data, payload, clean_sku),
        )

    # ---------------------------------------------------------------------------- stock

    @staticmethod
    def _require_numeric(value: str, *, field: str) -> int:
        try:
            return int(value)
        except ValueError as exc:
            raise AppError(
                slug="shopee-inventory-sync-failed",
                status_code=422,
                code="ERR_SHOPEE_INVENTORY_SYNC",
                title="Shopee Inventory Sync Failed",
                detail=f"Shopee {field} must be numeric, got '{value}'.",
            ) from exc

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
        """Set absolute stock for one Shopee model via ``/product/update_stock``."""
        clean_sku = (external_sku or "").strip()
        if not clean_sku:
            raise AppError(
                slug="shopee-inventory-sync-failed",
                status_code=422,
                code="ERR_SHOPEE_INVENTORY_SYNC",
                title="Shopee Inventory Sync Failed",
                detail="Shopee inventory sync requires a non-empty external_sku.",
            )
        item_id_text = (external_product_id or "").strip()
        if not item_id_text:
            raise AppError(
                slug="shopee-inventory-sync-failed",
                status_code=422,
                code="ERR_SHOPEE_INVENTORY_SYNC",
                title="Shopee Inventory Sync Failed",
                detail=(
                    f"Shopee cannot resolve an item_id from seller SKU '{clean_sku}'; pass "
                    "external_product_id from the channel mapping."
                ),
            )
        item_id = self._require_numeric(item_id_text, field="item_id")
        model_id_text = (external_variant_id or external_inventory_item_id or "").strip()
        #: Shopee uses model_id 0 for items that have no variations.
        model_id = self._require_numeric(model_id_text, field="model_id") if model_id_text else 0
        norm_qty = max(0, int(quantity))
        data = await self._request(
            "POST",
            UPDATE_STOCK_PATH,
            json_body={
                "item_id": item_id,
                "stock_list": [{"model_id": model_id, "seller_stock": [{"stock": norm_qty}]}],
            },
            error_slug="shopee-inventory-sync-failed",
            error_code="ERR_SHOPEE_INVENTORY_SYNC",
            error_title="Shopee Inventory Sync Failed",
        )
        return {
            "channel": "shopee",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "product_id": item_id,
            "variant_id": model_id,
            "location_id": location_id,
            "warehouse_id": warehouse_id,
            "raw": data,
        }

    # -------------------------------------------------------------------------- webhooks

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Constant-time HMAC-SHA256 check of a Shopee push callback."""
        partner_key = (secret or "").strip() or self._partner_key
        return verify_shopee_webhook(raw_body, headers, partner_key)
