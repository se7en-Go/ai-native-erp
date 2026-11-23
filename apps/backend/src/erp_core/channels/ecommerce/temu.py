"""Temu Open Platform connector: RPC router calls, order/product sync, signed webhooks.

Wire contract pinned by this adapter (nothing is guessed silently):

* Every call is a JSON ``POST`` to the regional router
  (``https://openapi-b-{us,eu,global}.temu.com/openapi/router``) carrying ``type``,
  ``app_key``, ``access_token``, ``mall_id``, ``timestamp``, ``data_type`` and ``sign``.
  The business payload travels in ``data``; the envelope is signed over the *unsigned*
  envelope only, so the credential never has to be re-serialized on the wire.
* ``sign`` is Temu's router fingerprint: the uppercase MD5 digest of
  ``secret + sorted(key+value)... + body + secret``. It is a request fingerprint, not a
  message authenticator; inbound webhooks use HMAC-SHA256 (``verify_temu_webhook``).
* ``fulfillment_mode`` selects the endpoint family, exactly as Temu splits the seller
  population: ``standard`` (platform-fulfilled -> tracking is *confirmed back* with
  ``bg.logistics.shipment.confirm``), ``semi_managed`` and ``local`` (seller-fulfilled ->
  shipments are *created* with ``bg.logistics.shipment.create`` and goods live under the
  ``bg.local.*`` namespace).
* Money never touches IEEE-754: JSON payloads are decoded with ``parse_float=Decimal``,
  a stray float raises ``ERR_TEMU_FLOAT_AMOUNT``, and every amount handed to the ERP is
  quantized to 2dp ``ROUND_HALF_UP``.
* ``app_secret``/``access_token`` are redacted from ``repr`` and from every ``AppError``
  detail (remote bodies are sanitized with the same secret list).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NoReturn

import httpx

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

TEMU_CHANNEL_TYPE = "temu"
TEMU_WEBHOOK_SIGNATURE_HEADER = "x-temu-signature"
TEMU_WEBHOOK_SIGNATURE_ALIAS_HEADER = "x-tm-signature"
TEMU_SUPPORTED_FULFILLMENT_MODES = frozenset({"standard", "semi_managed", "local"})
TEMU_PAGE_SIZE = 50

#: Regional routers. Temu splits compliance entities per region and a token only works on
#: the router that issued it, so an unknown region fails closed instead of guessing.
TEMU_ROUTER_ENDPOINTS: Mapping[str, str] = {
    "US": "https://openapi-b-us.temu.com/openapi/router",
    "EU": "https://openapi-b-eu.temu.com/openapi/router",
    "GLOBAL": "https://openapi-b-global.temu.com/openapi/router",
}
_REGION_CURRENCY: Mapping[str, str] = {"US": "USD", "EU": "EUR", "GLOBAL": "USD"}

_TEMU_ORDER_LIST_TYPE = "bg.order.list.get"
_TEMU_ORDER_DETAIL_TYPE = "bg.order.detail.get"
_TEMU_GOODS_LIST_TYPES: Mapping[str, str] = {
    "standard": "bg.goods.list.get",
    "semi_managed": "bg.local.goods.list.get",
    "local": "bg.local.goods.list.get",
}
_TEMU_GOODS_ADD_TYPES: Mapping[str, str] = {
    "standard": "bg.goods.add",
    "semi_managed": "bg.local.goods.add",
    "local": "bg.local.goods.add",
}
_TEMU_STOCK_TYPES: Mapping[str, str] = {
    "standard": "bg.goods.stock.update",
    "semi_managed": "bg.local.goods.stock.update",
    "local": "bg.local.goods.stock.update",
}
_TEMU_IMAGE_UPLOAD_TYPE = "bg.local.goods.image.upload"
_TEMU_SHIPMENT_CREATE_TYPE = "bg.logistics.shipment.create"
_TEMU_SHIPMENT_CONFIRM_TYPE = "bg.logistics.shipment.confirm"

#: Status tokens Temu returns on ``bg.order.list.get`` / ``bg.order.detail.get``.
_STATUS_MAP: Mapping[str, FulfillmentStatus] = {
    "PENDING": FulfillmentStatus.CONFIRMED_RESERVED,
    "UN_SHIPPED": FulfillmentStatus.CONFIRMED_RESERVED,
    "WAITING_SHIP": FulfillmentStatus.CONFIRMED_RESERVED,
    "PROCESSING": FulfillmentStatus.CONFIRMED_RESERVED,
    "SHIPPED": FulfillmentStatus.SHIPPED,
    "IN_TRANSIT": FulfillmentStatus.SHIPPED,
    "PARTIALLY_SHIPPED": FulfillmentStatus.PARTIALLY_SHIPPED,
    "DELIVERED": FulfillmentStatus.DELIVERED,
    "SIGNED": FulfillmentStatus.DELIVERED,
    "COMPLETED": FulfillmentStatus.DELIVERED,
    "EXCEPTION": FulfillmentStatus.EXCEPTION,
    "CANCELLED": FulfillmentStatus.CANCELLED,
    "CANCELED": FulfillmentStatus.CANCELLED,
    "REFUNDED": FulfillmentStatus.REFUNDED,
}

_CENT = Decimal("0.01")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Any epoch value above this is milliseconds, not seconds (1e11 s == year 5138).
_MILLISECOND_CUTOFF = Decimal("100000000000")
_REDACTION = "[redacted]"


def _app_error(
    *,
    slug: str,
    code: str,
    title: str,
    detail: str,
    status_code: int = 502,
) -> AppError:
    return AppError(
        slug=slug,
        status_code=status_code,
        code=code,
        title=title,
        detail=detail,
    )


# --------------------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------------------


def sign_temu_request(app_secret: str, params: Mapping[str, str], *, body: str = "") -> str:
    """Return Temu's router ``sign`` for ``params`` (uppercase MD5 hex digest).

    The digest is ``MD5(secret + concat(sorted(key + value)) + body + secret)`` over every
    parameter except ``sign`` itself. Sorting the key/value *pairs* makes the result
    independent of dict insertion order, which is why two callers cannot disagree about
    the same request.
    """
    if not app_secret:
        raise _app_error(
            slug="temu-signing-secret-missing",
            code="ERR_TEMU_SIGNING_SECRET_MISSING",
            title="Temu Signing Secret Missing",
            detail=(
                "Temu router calls are signed with the app_secret; an empty secret cannot "
                "produce a verifiable request."
            ),
        )
    canonical = "".join(f"{key}{value}" for key, value in sorted(params.items()) if key != "sign")
    material = f"{app_secret}{canonical}{body}{app_secret}".encode()
    # `usedforsecurity=False`: this is Temu's documented router fingerprint over public
    # parameters, not a message authenticator (inbound webhooks use HMAC-SHA256 below).
    return hashlib.md5(material, usedforsecurity=False).hexdigest().upper()


def verify_temu_webhook(raw_body: bytes, headers: Mapping[str, str], app_secret: str) -> bool:
    """Constant-time HMAC-SHA256 check of Temu's ``x-temu-signature`` webhook header."""
    if not app_secret:
        return False
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    received = (
        lowered.get(TEMU_WEBHOOK_SIGNATURE_HEADER)
        or lowered.get(TEMU_WEBHOOK_SIGNATURE_ALIAS_HEADER)
        or ""
    ).strip()
    if not received:
        return False
    if received.lower().startswith("sha256="):
        received = received.split("=", 1)[1].strip()
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.lower())


# --------------------------------------------------------------------------------------
# Decimal / time primitives (no floats anywhere near money)
# --------------------------------------------------------------------------------------


def _decode_json(text: str) -> dict[str, Any]:
    """Decode a JSON object with every JSON float already materialised as ``Decimal``."""
    try:
        decoded = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise _app_error(
            slug="temu-response-malformed",
            code="ERR_TEMU_RESPONSE_MALFORMED",
            title="Temu Response Malformed",
            detail=f"Temu returned a non-JSON body ({exc.msg} at column {exc.colno}).",
        ) from exc
    if not isinstance(decoded, dict):
        raise _app_error(
            slug="temu-response-malformed",
            code="ERR_TEMU_RESPONSE_MALFORMED",
            title="Temu Response Malformed",
            detail=f"Temu returned a JSON {type(decoded).__name__} instead of an object.",
        )
    return decoded


def _decimal(value: Any, *, field: str) -> Decimal:
    """Convert a Temu scalar to ``Decimal``; a float is a hard failure, never a coercion."""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise _app_error(
            slug="temu-float-amount",
            code="ERR_TEMU_FLOAT_AMOUNT",
            title="Temu Float Amount Rejected",
            detail=(
                f"Temu returned an IEEE-754 float for '{field}'; money must arrive as text "
                "or Decimal so binary rounding cannot leak into ledger totals."
            ),
        )
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise _app_error(
                slug="temu-invalid-amount",
                code="ERR_TEMU_INVALID_AMOUNT",
                title="Temu Invalid Amount",
                detail=f"Temu returned '{value}' for '{field}', which is not a decimal number.",
            ) from exc
    raise _app_error(
        slug="temu-invalid-amount",
        code="ERR_TEMU_INVALID_AMOUNT",
        title="Temu Invalid Amount",
        detail=f"Unsupported numeric type {type(value).__name__} for '{field}'.",
    )


def _money(value: Any, *, field: str = "amount") -> Decimal:
    """Quantize a Temu amount to exact 2dp ``ROUND_HALF_UP``."""
    return _decimal(value, field=field).quantize(_CENT, rounding=ROUND_HALF_UP)


def _first_money(source: Mapping[str, Any], *keys: str, field: str) -> Decimal:
    for key in keys:
        if key in source:
            candidate = source[key]
            if candidate is not None and candidate != "":
                return _decimal(candidate, field=field)
    return Decimal("0")


def _unix_seconds(moment: datetime) -> int:
    return int((moment - _EPOCH) // timedelta(seconds=1))


def _unix_milliseconds(moment: datetime) -> int:
    return int((moment - _EPOCH) // timedelta(milliseconds=1))


def _epoch_to_datetime(value: Any) -> datetime:
    """Convert a Temu epoch scalar (seconds or milliseconds) to an aware UTC datetime."""
    epoch = _decimal(value, field="update_time")
    if epoch <= Decimal("0"):
        return datetime.now(UTC)
    if epoch > _MILLISECOND_CUTOFF:
        epoch = epoch / 1000
    whole = int(epoch)
    micros = int((epoch - Decimal(whole)) * Decimal(1_000_000))
    return datetime.fromtimestamp(whole, UTC) + timedelta(microseconds=micros)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Temu request payload cannot serialize {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class _ListingSubmission:
    """Adapter-internal, already-normalized listing request (one wire shape for both APIs)."""

    external_sku: str
    title: str
    description: str
    bullet_points: tuple[str, ...]
    price_usd: Decimal
    quantity: int
    images: tuple[str, ...]
    category: str
    hs_code: str | None
    options: tuple[Mapping[str, Any], ...]
    variants: tuple[Mapping[str, Any], ...]
    image_blobs: tuple[Mapping[str, Any], ...]
    moq_tiers: tuple[Mapping[str, Any], ...]


class TemuAdapter(CommercePlatformAdapter):
    """Temu Open Platform adapter for ``standard``, ``semi_managed`` and ``local`` sellers.

    Credentials are held privately and never appear in ``repr``/``str``, log records or
    ``AppError`` details; only ``mall_id``/``app_key``/``region``/``fulfillment_mode`` are
    safe to echo.
    """

    def __init__(
        self,
        *,
        mall_id: str,
        app_key: str,
        app_secret: str,
        access_token: str,
        region: str = "US",
        fulfillment_mode: str = "standard",
        endpoint_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.mall_id = mall_id.strip()
        self.app_key = app_key.strip()
        self.region = region.strip().upper()
        mode = fulfillment_mode.strip().lower()
        if mode not in TEMU_SUPPORTED_FULFILLMENT_MODES:
            raise _app_error(
                slug="temu-fulfillment-mode-invalid",
                code="ERR_TEMU_FULFILLMENT_MODE_INVALID",
                title="Temu Fulfillment Mode Invalid",
                detail=(
                    f"fulfillment_mode must be one of "
                    f"{', '.join(sorted(TEMU_SUPPORTED_FULFILLMENT_MODES))} "
                    f"(got '{fulfillment_mode}')."
                ),
                status_code=422,
            )
        self.fulfillment_mode = mode
        default_endpoint = TEMU_ROUTER_ENDPOINTS.get(self.region)
        if endpoint_url is None and default_endpoint is None:
            raise _app_error(
                slug="temu-region-unsupported",
                code="ERR_TEMU_REGION_UNSUPPORTED",
                title="Temu Region Unsupported",
                detail=(
                    f"No Temu router is known for region '{region}'; supported regions are "
                    f"{', '.join(sorted(TEMU_ROUTER_ENDPOINTS))}."
                ),
                status_code=422,
            )
        self.endpoint_url = (endpoint_url or str(default_endpoint)).rstrip("/")
        self._app_secret = app_secret.strip()
        self._access_token = access_token.strip()
        if not self.mall_id or not self.app_key or not self._app_secret or not self._access_token:
            raise _app_error(
                slug="temu-credentials-invalid",
                code="ERR_TEMU_CREDENTIALS_INVALID",
                title="Temu Credentials Invalid",
                detail=(
                    "A Temu connection needs non-empty mall_id, app_key, app_secret and "
                    "access_token."
                ),
                status_code=422,
            )
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"TemuAdapter(mall_id={self.mall_id!r}, app_key={self.app_key!r}, "
            f"region={self.region!r}, fulfillment_mode={self.fulfillment_mode!r}, "
            f"endpoint_url={self.endpoint_url!r}, app_secret='{_REDACTION}', "
            f"access_token='{_REDACTION}')"
        )

    # ------------------------------------------------------------------ transport plumbing

    def _secrets(self) -> tuple[str, ...]:
        """Every value that must never reach a log line or an ``AppError`` detail."""
        return tuple(value for value in (self._app_secret, self._access_token) if value)

    def _redacted(self, text: str, limit: int = 400) -> str:
        cleaned = text[:limit]
        for secret in self._secrets():
            cleaned = cleaned.replace(secret, _REDACTION)
        return cleaned

    def _raise_remote(self, payload: Mapping[str, Any], api_type: str) -> NoReturn:
        code = str(payload.get("errorCode") or payload.get("code") or "").strip()
        message = str(payload.get("errorMsg") or payload.get("message") or "").strip()
        raise _app_error(
            slug="temu-api-error",
            code="ERR_TEMU_API_ERROR",
            title="Temu API Error",
            detail=self._redacted(
                f"{api_type} was rejected by Temu"
                f" ({code or 'no errorCode'}): {message or json.dumps(payload, default=str)}"
            ),
        )

    def _parse_response(self, response: httpx.Response, api_type: str) -> dict[str, Any]:
        if response.status_code == 429:
            raise _app_error(
                slug="temu-rate-limited",
                code="ERR_TEMU_RATE_LIMITED",
                title="Temu Rate Limited",
                detail=(
                    f"{api_type} hit Temu's per-mall/app rate limit (HTTP 429); retry after "
                    "the advertised backoff."
                ),
                status_code=429,
            )
        if response.status_code in (401, 403):
            raise _app_error(
                slug="temu-auth-failed",
                code="ERR_TEMU_AUTH_FAILED",
                title="Temu Authentication Failed",
                detail=(
                    f"Temu rejected the access_token/app_key for {api_type} "
                    f"(HTTP {response.status_code})."
                ),
            )
        if response.status_code >= 400:
            raise _app_error(
                slug="temu-api-error",
                code="ERR_TEMU_API_ERROR",
                title="Temu API Error",
                detail=self._redacted(
                    f"HTTP {response.status_code} from {api_type}: {response.text}"
                ),
            )
        payload = _decode_json(response.text)
        if payload.get("success") is False or payload.get("errorCode") or payload.get("code"):
            self._raise_remote(payload, api_type)
        result = payload.get("result")
        return dict(result) if isinstance(result, Mapping) else {}

    async def _post_router(self, api_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """POST one signed RPC call to the regional router and return ``result``."""
        envelope: dict[str, Any] = {
            "type": api_type,
            "app_key": self.app_key,
            "access_token": self._access_token,
            "mall_id": self.mall_id,
            "region": self.region,
            "timestamp": str(_unix_seconds(datetime.now(UTC))),
            "data_type": "JSON",
            "data": dict(data),
        }
        unsigned_body = json.dumps(envelope, separators=(",", ":"), default=_json_default)
        signature_params = {key: str(value) for key, value in envelope.items() if key != "data"}
        envelope["sign"] = sign_temu_request(self._app_secret, signature_params, body=unsigned_body)
        body = json.dumps(envelope, separators=(",", ":"), default=_json_default)
        headers = {
            "Content-Type": "application/json",
            "x-temu-app-key": self.app_key,
            "x-temu-access-token": self._access_token,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(self.endpoint_url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise _app_error(
                slug="temu-transport-error",
                code="ERR_TEMU_TRANSPORT",
                title="Temu Transport Error",
                detail=self._redacted(f"{type(exc).__name__} calling {api_type}: {exc}"),
            ) from exc
        return self._parse_response(response, api_type)

    # ------------------------------------------------------------------ normalization

    def _default_currency(self) -> str:
        return _REGION_CURRENCY.get(self.region, "USD")

    def _normalize_line_item(self, row: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Build one line item, deriving the line total from unit price x quantity."""
        sku = str(
            row.get("extCode")
            or row.get("sellerSku")
            or row.get("skuId")
            or row.get("productSkcId")
            or ""
        ).strip()
        if not sku:
            return None
        raw_quantity = row.get("quantity", row.get("goodsQuantity"))
        quantity = max(1, int(_decimal(raw_quantity, field="quantity")) if raw_quantity else 1)
        unit_price = _first_money(row, "goodsPrice", "price", field="goods_price")
        explicit_total = row.get("goodsAmount") or row.get("totalAmount")
        line_total = (
            _money(explicit_total, field="goods_amount")
            if explicit_total not in (None, "")
            else (unit_price * Decimal(quantity)).quantize(_CENT, rounding=ROUND_HALF_UP)
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(row.get("goodsName") or row.get("productName") or sku),
            quantity=quantity,
            line_total=line_total,
        )

    def _normalize_order(self, row: Mapping[str, Any]) -> NormalizedOrder | None:
        """Build one normalized order, or ``None`` when Temu returned no usable line item."""
        amount = _mapping(row.get("orderAmount")) or _mapping(row.get("payment"))
        status = str(row.get("orderStatus") or row.get("parentOrderStatus") or "").upper()
        order_sn = str(row.get("orderSn") or row.get("orderNumber") or "").strip()
        currency = (
            str(amount.get("currency") or row.get("currency") or self._default_currency())
            .strip()
            .upper()
        )
        commission = _first_money(
            amount, "platformCommission", "commissionAmount", field="platform_commission"
        ) or _first_money(
            row, "platformCommission", "commissionAmount", field="platform_commission"
        )
        commission_base = commission.quantize(_CENT, rounding=ROUND_HALF_UP)
        refunded = _first_money(
            amount, "refundAmount", "sellerRefundAmount", field="refund_amount"
        ) or _first_money(row, "refundAmount", field="refund_amount")
        metadata: dict[str, Any] = {
            "temu_order_sn": order_sn,
            "temu_parent_order_sn": row.get("parentOrderSn") or order_sn,
            "temu_order_status": status,
            "temu_mall_id": self.mall_id,
            "temu_region": self.region,
            "temu_fulfillment_mode": self.fulfillment_mode,
        }
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded,
            platform_commission_base=commission,
        )
        country = str(row.get("countryCode") or row.get("regionCode") or "").strip().upper()
        buyer_id = str(row.get("buyerId") or row.get("buyerUserId") or "").strip()
        goods_rows = row.get("orderItems") or row.get("goodsList") or []
        line_items = [
            item
            for goods in goods_rows
            if isinstance(goods, Mapping) and (item := self._normalize_line_item(goods)) is not None
        ]
        if not line_items:
            # A Temu order with no resolvable SKU can never be reserved or fulfilled, so it
            # is skipped instead of importing an order the ERP cannot act on.
            return None
        return NormalizedOrder(
            external_order_id=order_sn,
            external_order_number=str(row.get("parentOrderSn") or order_sn),
            order_mode=(
                OrderMode.B2B_WHOLESALE
                if str(row.get("orderType") or "").upper() == "B2B"
                else OrderMode.B2C_RETAIL
            ),
            currency=currency,
            subtotal_amount=_first_money(
                amount, "goodsAmount", "subTotal", "subtotal", field="subtotal"
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            shipping_charged=_first_money(
                amount, "shippingAmount", "postage", field="shipping"
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            discount_amount=_first_money(
                amount, "discountAmount", "sellerDiscount", field="discount"
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            tax_collected=_first_money(amount, "taxAmount", "tax", field="tax").quantize(
                _CENT, rounding=ROUND_HALF_UP
            ),
            total_amount=_first_money(
                amount, "orderTotalAmount", "totalAmount", "orderAmount", field="total"
            ).quantize(_CENT, rounding=ROUND_HALF_UP),
            platform_commission_base=commission_base,
            fulfillment_status=_STATUS_MAP.get(status, FulfillmentStatus.CONFIRMED_RESERVED),
            payment_status=(
                PaymentStatus.REFUNDED
                if status in {"CANCELLED", "CANCELED", "REFUNDED"}
                else PaymentStatus.PAID
            ),
            customer=NormalizedCustomer(
                external_customer_id=buyer_id or None,
                # Temu exposes no ISO buyer identity to sellers without the sensitive
                # decrypt scope, so the buyer id is kept as an opaque visitor identity.
                external_identity_type=IdentityType.WEB_VISITOR_ID,
                display_name=str(row.get("buyerName") or "Temu Buyer"),
                country_code=country if len(country) == 2 else None,
            ),
            line_items=line_items,
            channel_metadata=metadata,
            updated_at=_epoch_to_datetime(
                row.get("updateTime") or row.get("updateTimeMs") or row.get("createTime")
            ),
        )

    @staticmethod
    def _next_cursor(result: Mapping[str, Any], page_number: int, page_size: int) -> str | None:
        total = result.get("totalItemNum", result.get("total"))
        if total is not None and _decimal(total, field="total") > Decimal(page_number * page_size):
            return str(page_number + 1)
        return None

    # ------------------------------------------------------------------ orders / products

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        """List orders whose update window starts at ``since``, then load full details."""
        page_number = int(cursor) if cursor and cursor.strip().isdigit() else 1
        listed = await self._post_router(
            _TEMU_ORDER_LIST_TYPE,
            {
                "mallId": self.mall_id,
                "regionId": self.region,
                "updateTimeStart": _unix_milliseconds(since),
                "updateTimeEnd": _unix_milliseconds(datetime.now(UTC)),
                "pageNumber": page_number,
                "pageSize": TEMU_PAGE_SIZE,
            },
        )
        rows = listed.get("orderList") or listed.get("list") or []
        order_sns = [
            str(row["orderSn"]).strip()
            for row in rows
            if isinstance(row, Mapping) and str(row.get("orderSn") or "").strip()
        ]
        next_cursor = self._next_cursor(listed, page_number, TEMU_PAGE_SIZE)
        if not order_sns:
            return [], next_cursor
        detail = await self._post_router(
            _TEMU_ORDER_DETAIL_TYPE,
            {"mallId": self.mall_id, "orderSnList": order_sns},
        )
        detail_rows = detail.get("orderList") or detail.get("list") or []
        if not detail_rows and isinstance(detail.get("orderInfo"), Mapping):
            detail_rows = [detail["orderInfo"]]
        orders: list[NormalizedOrder] = []
        for row in detail_rows:
            if not isinstance(row, Mapping):
                continue
            normalized = self._normalize_order(row)
            if normalized is not None:
                orders.append(normalized)
        return orders, next_cursor

    def _normalize_product(self, row: Mapping[str, Any]) -> NormalizedProduct | None:
        product_id = str(
            row.get("goodsId") or row.get("productId") or row.get("productSkcId") or ""
        ).strip()
        if not product_id:
            return None
        sku_rows = row.get("skuList") or row.get("skuInfoList") or []
        variants: list[dict[str, Any]] = []
        for sku in sku_rows:
            if not isinstance(sku, Mapping):
                continue
            sku_code = str(
                sku.get("extCode") or sku.get("sellerSku") or sku.get("skuId") or ""
            ).strip()
            if not sku_code:
                continue
            quantity = sku.get("quantity", sku.get("stock"))
            variants.append(
                {
                    "external_sku": sku_code,
                    "external_variant_id": str(sku.get("skuId") or "").strip() or None,
                    "external_inventory_item_id": (
                        str(sku.get("inventoryItemId") or "").strip() or None
                    ),
                    "price": str(_money(sku.get("price") or sku.get("goodsPrice"), field="price")),
                    "quantity": max(0, int(_decimal(quantity, field="quantity")))
                    if quantity not in (None, "")
                    else 0,
                }
            )
        return NormalizedProduct(
            external_product_id=product_id,
            title=str(row.get("goodsName") or row.get("productName") or product_id),
            variants=variants,
        )

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        """List goods (``bg.local.*`` for seller-managed modes) updated after ``since``."""
        page_number = int(cursor) if cursor and cursor.strip().isdigit() else 1
        listed = await self._post_router(
            _TEMU_GOODS_LIST_TYPES[self.fulfillment_mode],
            {
                "mallId": self.mall_id,
                "regionId": self.region,
                "updateTimeStart": _unix_milliseconds(since),
                "updateTimeEnd": _unix_milliseconds(datetime.now(UTC)),
                "pageNumber": page_number,
                "pageSize": TEMU_PAGE_SIZE,
            },
        )
        rows = listed.get("goodsList") or listed.get("list") or []
        products = [
            product
            for row in rows
            if isinstance(row, Mapping) and (product := self._normalize_product(row)) is not None
        ]
        return products, self._next_cursor(listed, page_number, TEMU_PAGE_SIZE)

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        """Create a shipment (seller-managed) or confirm tracking (platform-fulfilled)."""
        order_sn = external_order_id.strip()
        tracking = tracking_number.strip()
        carrier = carrier_company.strip()
        if not order_sn or not tracking or not carrier:
            raise _app_error(
                slug="temu-fulfillment-incomplete",
                code="ERR_TEMU_FULFILLMENT_INCOMPLETE",
                title="Temu Fulfillment Incomplete",
                detail=(
                    "Temu fulfillment requires a non-empty order id, tracking number and carrier."
                ),
                status_code=422,
            )
        api_type = (
            _TEMU_SHIPMENT_CONFIRM_TYPE
            if self.fulfillment_mode == "standard"
            else _TEMU_SHIPMENT_CREATE_TYPE
        )
        data: dict[str, Any] = {
            "mallId": self.mall_id,
            "orderSn": order_sn,
            "trackingNumber": tracking,
            "shippingCompany": carrier,
            "notifyCustomer": notify_customer,
        }
        if tracking_url:
            data["trackingUrl"] = tracking_url.strip()
        if fulfillment_order_id:
            data["packageSn"] = fulfillment_order_id.strip()
        result = await self._post_router(api_type, data)
        return {
            "channel": TEMU_CHANNEL_TYPE,
            "external_order_id": order_sn,
            "fulfillment_order_id": fulfillment_order_id,
            "tracking_number": tracking,
            "carrier_company": carrier,
            "tracking_url": tracking_url,
            "fulfillment_mode": self.fulfillment_mode,
            "notify_customer": notify_customer,
            "api_type": api_type,
            "raw": result,
        }

    # ------------------------------------------------------------------ listing publish

    @staticmethod
    def _submission_from_legacy(legacy: Mapping[str, Any]) -> _ListingSubmission:
        """Accept the keyword connector signature the dispatcher also drives."""
        return _ListingSubmission(
            external_sku=str(legacy.get("sku") or ""),
            title=str(legacy.get("title") or ""),
            description=str(legacy.get("description") or ""),
            bullet_points=tuple(str(point) for point in legacy.get("bullet_points") or ()),
            price_usd=_decimal(legacy.get("price_usd"), field="price_usd"),
            quantity=int(_decimal(legacy.get("quantity"), field="quantity")),
            images=tuple(str(url) for url in legacy.get("images") or ()),
            category=str(legacy.get("category") or legacy.get("category_id") or ""),
            hs_code=(str(legacy["hs_code"]).strip() or None) if legacy.get("hs_code") else None,
            options=tuple(
                option for option in legacy.get("options") or () if isinstance(option, Mapping)
            ),
            variants=tuple(
                variant for variant in legacy.get("variants") or () if isinstance(variant, Mapping)
            ),
            image_blobs=tuple(
                blob for blob in legacy.get("image_blobs") or () if isinstance(blob, Mapping)
            ),
            moq_tiers=tuple(
                tier for tier in legacy.get("moq_tiers") or () if isinstance(tier, Mapping)
            ),
        )

    @staticmethod
    def _submission_from_payload(payload: MarketplaceListingPayload) -> _ListingSubmission:
        return _ListingSubmission(
            external_sku=payload.external_sku.strip(),
            title=payload.title.strip(),
            description=payload.description,
            bullet_points=tuple(payload.bullet_points),
            price_usd=payload.price_usd,
            quantity=payload.quantity,
            images=tuple(payload.images),
            category=payload.category,
            hs_code=payload.hs_code,
            options=tuple(
                {"name": option.name, "values": list(option.values)} for option in payload.options
            ),
            variants=tuple(
                {
                    "local_sku": variant.local_sku,
                    "external_sku": variant.external_sku,
                    "option_values": dict(variant.option_values),
                    "price_usd": variant.price_usd,
                    "quantity": variant.quantity,
                    "image_url": variant.image_url,
                }
                for variant in payload.variants
            ),
            image_blobs=tuple(
                {
                    "filename": blob.filename,
                    "mime_type": blob.mime_type,
                    "data": blob.data,
                    "alt_text": blob.alt_text,
                }
                for blob in payload.image_blobs
            ),
            moq_tiers=tuple(
                {"min_qty": tier.min_qty, "unit_price_usd": tier.unit_price_usd}
                for tier in payload.moq_tiers
            ),
        )

    @staticmethod
    def _listing_submission(
        payload: MarketplaceListingPayload | None, legacy: Mapping[str, Any]
    ) -> _ListingSubmission:
        if payload is not None and legacy:
            raise _app_error(
                slug="temu-listing-arguments",
                code="ERR_TEMU_LISTING_ARGUMENTS",
                title="Temu Listing Arguments Ambiguous",
                detail=(
                    "publish_listing accepts either a MarketplaceListingPayload or the "
                    "keyword connector arguments, never both."
                ),
                status_code=422,
            )
        if payload is not None:
            return TemuAdapter._submission_from_payload(payload)
        if not legacy:
            raise _app_error(
                slug="temu-listing-arguments",
                code="ERR_TEMU_LISTING_ARGUMENTS",
                title="Temu Listing Arguments Missing",
                detail="publish_listing needs a MarketplaceListingPayload or listing keywords.",
                status_code=422,
            )
        return TemuAdapter._submission_from_legacy(legacy)

    async def _upload_image(self, blob: Mapping[str, Any]) -> str | None:
        raw = blob.get("data")
        if not raw:
            return None
        encoded = base64.b64encode(bytes(raw)).decode()
        result = await self._post_router(
            _TEMU_IMAGE_UPLOAD_TYPE,
            {
                "mallId": self.mall_id,
                "fileName": str(blob.get("filename") or "listing.jpg"),
                "mimeType": str(blob.get("mime_type") or "image/jpeg"),
                "imageBase64": encoded,
            },
        )
        url = str(result.get("imageUrl") or result.get("url") or "").strip()
        return url or None

    @staticmethod
    def _category_field(category: str) -> dict[str, Any]:
        cleaned = category.strip()
        if not cleaned:
            return {}
        if cleaned.isdigit():
            return {"catId": cleaned}
        return {"categoryName": cleaned[:256]}

    async def publish_listing(
        self,
        payload: MarketplaceListingPayload | None = None,
        **legacy: Any,
    ) -> MarketplaceListingDispatchReceipt:
        """Create one single- or multi-variant Temu listing and return the assigned ids."""
        submission = self._listing_submission(payload, legacy)
        clean_sku = submission.external_sku.strip()
        clean_title = submission.title.strip()
        if not clean_sku or not clean_title:
            raise _app_error(
                slug="temu-listing-rejected",
                code="ERR_TEMU_LISTING_REJECTED",
                title="Temu Listing Rejected",
                detail=(
                    "A Temu listing needs a non-empty external SKU and title before it can be "
                    "published."
                ),
                status_code=422,
            )
        currency = self._default_currency()
        image_urls = [
            url.strip() for url in submission.images if url.strip().startswith("https://")
        ]
        for blob in submission.image_blobs:
            uploaded = await self._upload_image(blob)
            if uploaded:
                image_urls.append(uploaded)

        sku_rows: list[dict[str, Any]] = []
        for variant in submission.variants:
            variant_sku = str(
                variant.get("external_sku") or variant.get("local_sku") or clean_sku
            ).strip()
            price = _money(variant.get("price_usd") or submission.price_usd, field="price")
            raw_quantity = variant.get("quantity")
            variant_quantity = (
                int(_decimal(raw_quantity, field="quantity"))
                if raw_quantity not in (None, "")
                else submission.quantity
            )
            specs = [
                {"specKey": str(key), "specValue": str(value)}
                for key, value in _mapping(variant.get("option_values")).items()
            ]
            image_url = str(variant.get("image_url") or "").strip()
            sku_rows.append(
                {
                    "extCode": variant_sku,
                    "price": str(price),
                    "currency": currency,
                    "quantity": max(0, variant_quantity),
                    **({"specList": specs} if specs else {}),
                    **({"images": [image_url]} if image_url.startswith("https://") else {}),
                }
            )
        if not sku_rows:
            sku_rows.append(
                {
                    "extCode": clean_sku,
                    "price": str(_money(submission.price_usd, field="price_usd")),
                    "currency": currency,
                    "quantity": max(0, submission.quantity),
                }
            )

        description_lines = [submission.description.strip()]
        description_lines.extend(
            f"- {point.strip()}" for point in submission.bullet_points if point.strip()
        )
        body: dict[str, Any] = {
            "mallId": self.mall_id,
            "regionId": self.region,
            "goodsName": clean_title[:255],
            "goodsDesc": "\n".join(line for line in description_lines if line)[:10000],
            "skuList": sku_rows,
            **self._category_field(submission.category),
        }
        if image_urls:
            body["goodsImage"] = image_urls[:9]
        if submission.hs_code:
            body["hsCode"] = submission.hs_code
        if submission.options:
            body["goodsSpecList"] = [
                {
                    "specName": str(option.get("name") or ""),
                    "specValues": [str(value) for value in option.get("values") or ()],
                }
                for option in submission.options
            ]
        if submission.moq_tiers:
            body["priceLadder"] = [
                {
                    "minQuantity": int(_decimal(tier.get("min_qty"), field="min_qty")),
                    "unitPrice": str(
                        _money(
                            tier.get("unit_price_usd") or tier.get("unit_price"),
                            field="unit_price",
                        )
                    ),
                }
                for tier in submission.moq_tiers
            ]

        result = await self._post_router(_TEMU_GOODS_ADD_TYPES[self.fulfillment_mode], body)
        goods_id = str(result.get("goodsId") or result.get("productId") or "").strip()
        if not goods_id:
            raise _app_error(
                slug="temu-listing-rejected",
                code="ERR_TEMU_LISTING_REJECTED",
                title="Temu Listing Rejected",
                detail=(
                    f"Temu returned no goodsId for '{clean_sku}': "
                    f"{self._redacted(json.dumps(result, default=str))}"
                ),
            )
        receipt_rows = result.get("skuList") or result.get("skuInfoList") or []
        variant_receipts = [
            MarketplaceVariantReceipt(
                external_sku=str(row.get("extCode") or row.get("sellerSku") or clean_sku),
                external_variant_id=str(row.get("skuId") or "").strip() or None,
                external_inventory_item_id=(str(row.get("inventoryItemId") or "").strip() or None),
            )
            for row in receipt_rows
            if isinstance(row, Mapping)
        ]
        return MarketplaceListingDispatchReceipt(
            external_product_id=goods_id,
            external_sku=clean_sku,
            status=str(result.get("goodsStatus") or result.get("status") or "PUBLISHED"),
            variant_receipts=variant_receipts,
            raw_response=result,
        )

    # ------------------------------------------------------------------ inventory

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
        """Push one clamped stock level to Temu and echo the ids that were targeted."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise _app_error(
                slug="temu-inventory-sync-failed",
                code="ERR_TEMU_INVENTORY_SYNC",
                title="Temu Inventory Sync Failed",
                detail="Temu inventory sync requires a non-empty external_sku.",
                status_code=422,
            )
        norm_qty = max(0, int(quantity))
        target: dict[str, Any] = {
            "mallId": self.mall_id,
            "regionId": self.region,
            "extCode": clean_sku,
            "quantity": norm_qty,
        }
        for key, value in (
            ("goodsId", external_product_id),
            ("skuId", external_variant_id),
            ("inventoryItemId", external_inventory_item_id),
            ("warehouseId", warehouse_id),
            ("locationId", location_id),
        ):
            cleaned = (value or "").strip()
            if cleaned:
                target[key] = cleaned
        api_type = _TEMU_STOCK_TYPES[self.fulfillment_mode]
        result = await self._post_router(api_type, target)
        return {
            "channel": TEMU_CHANNEL_TYPE,
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_product_id": external_product_id,
            "external_variant_id": external_variant_id,
            "external_inventory_item_id": external_inventory_item_id,
            "warehouse_id": warehouse_id,
            "location_id": location_id,
            "api_type": api_type,
            "raw": result,
        }

    # ------------------------------------------------------------------ webhooks

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """Constant-time HMAC-SHA256 verification, falling back to the connection secret."""
        return verify_temu_webhook(raw_body, headers, secret or self._app_secret)


__all__ = [
    "TEMU_CHANNEL_TYPE",
    "TEMU_PAGE_SIZE",
    "TEMU_ROUTER_ENDPOINTS",
    "TEMU_SUPPORTED_FULFILLMENT_MODES",
    "TEMU_WEBHOOK_SIGNATURE_ALIAS_HEADER",
    "TEMU_WEBHOOK_SIGNATURE_HEADER",
    "TemuAdapter",
    "sign_temu_request",
    "verify_temu_webhook",
]
