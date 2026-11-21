"""TikTok Shop Core connector: signed Open API calls, BYOK custom apps, webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

TIKTOK_API_BASE = "https://open-api.tiktokglobalshop.com"
TIKTOK_CUSTOM_APP_SHOP_LIMIT = 25
_UNSIGNED_PARAMS = frozenset({"sign", "access_token"})
_STATUS_MAP = {
    "AWAITING_SHIPMENT": FulfillmentStatus.CONFIRMED_RESERVED,
    "AWAITING_COLLECTION": FulfillmentStatus.CONFIRMED_RESERVED,
    "IN_TRANSIT": FulfillmentStatus.SHIPPED,
    "DELIVERED": FulfillmentStatus.DELIVERED,
    "COMPLETED": FulfillmentStatus.DELIVERED,
    "CANCELLED": FulfillmentStatus.CANCELLED,
}


def sign_tiktok_request(
    app_secret: str,
    path: str,
    query_params: Mapping[str, str],
    body_str: str = "",
) -> str:
    """TTSPC signature: HMAC-SHA256(secret, secret + path + sorted(k+v) + body + secret)."""
    if not app_secret:
        raise ValueError("app_secret is required to sign TikTok Shop requests")
    canonical = path + "".join(
        f"{key}{value}"
        for key, value in sorted(query_params.items())
        if key not in _UNSIGNED_PARAMS
    )
    message = f"{app_secret}{canonical}{body_str}{app_secret}"
    return hmac.new(app_secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_tiktok_webhook_signature(
    raw_body: bytes, headers: Mapping[str, str], app_key: str, app_secret: str
) -> bool:
    received = headers.get("Authorization") or headers.get("authorization") or ""
    if not received or not app_key or not app_secret:
        return False
    expected = hmac.new(
        app_secret.encode(), app_key.encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received.strip())


class TikTokShopCoreAdapter(CommercePlatformAdapter):
    """TikTok Shop Core API adapter for seller BYOK custom apps."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str,
        shop_cipher: str,
        app_mode: str = "seller_byok",
        http_client: httpx.AsyncClient | None = None,
        base_url: str = TIKTOK_API_BASE,
        webhook_secret: str = "",
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.shop_cipher = shop_cipher
        self.app_mode = app_mode
        self._http_client = http_client
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret

    async def _signed_request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = {
            "app_key": self.app_key,
            "shop_cipher": self.shop_cipher,
            "timestamp": str(int(time.time())),
            **(query or {}),
        }
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        params["sign"] = sign_tiktok_request(self.app_secret, path, params, body_str)
        url = f"{self.base_url}{path}?{urlencode(params)}"
        headers = {
            "x-tts-access-token": self.access_token,
            "Content-Type": "application/json",
        }
        if self._http_client is not None:
            response = await self._http_client.request(
                method, url, content=body_str or None, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, url, content=body_str or None, headers=headers
                )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or int(payload.get("code", 0)) != 0:
            raise AppError(
                slug="tiktok-shop-api-error",
                status_code=502,
                code="ERR_TIKTOK_SHOP_API",
                title="TikTok Shop API Error",
                detail=json.dumps(payload)[:500],
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value if value not in (None, "") else "0"))

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Build one line item, multiplying TikTok's per-unit ``sale_price`` by quantity.

        TikTok Shop's ``sale_price`` is the unit price; using it as the line total silently
        under-reports multi-unit lines (and therefore order revenue and CVP).
        """
        sku = str(item.get("seller_sku") or "").strip()
        if not sku:
            return None
        quantity = int(item.get("quantity") or 1)
        unit_price = self._decimal(item.get("sale_price"))
        explicit_total = item.get("line_total") or item.get("total_price")
        line_total = (
            self._decimal(explicit_total)
            if explicit_total not in (None, "")
            else (unit_price * Decimal(quantity)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(item.get("product_name") or sku),
            quantity=quantity,
            line_total=line_total,
        )

    def _normalize_order(self, order: dict[str, Any]) -> NormalizedOrder:
        payment = order.get("payment") or {}
        recipient = order.get("recipient_address") or {}
        status = str(order.get("status") or "")
        metadata: dict[str, Any] = {
            "tiktok_order_status": status,
            "tiktok_user_id": order.get("user_id"),
        }
        if order.get("packages"):
            metadata["tiktok_package_id"] = (order["packages"][0] or {}).get("id")
        return NormalizedOrder(
            external_order_id=str(order["id"]),
            external_order_number=str(order.get("id")),
            order_mode=OrderMode.B2C_RETAIL,
            currency=str(payment.get("currency") or "USD"),
            subtotal_amount=self._decimal(payment.get("sub_total")),
            shipping_charged=self._decimal(payment.get("shipping_fee")),
            discount_amount=self._decimal(payment.get("platform_discount")),
            tax_collected=self._decimal(payment.get("tax")),
            total_amount=self._decimal(payment.get("total_amount")),
            fulfillment_status=_STATUS_MAP.get(status, FulfillmentStatus.CONFIRMED_RESERVED),
            payment_status=(
                PaymentStatus.REFUNDED if status == "CANCELLED" else PaymentStatus.PAID
            ),
            customer=NormalizedCustomer(
                email=order.get("buyer_email"),
                phone_e164=recipient.get("phone_number"),
                external_customer_id=str(order.get("user_id") or "") or None,
                external_identity_type=IdentityType.TIKTOK_BUYER_ID,
                display_name=str(
                    recipient.get("name") or order.get("buyer_email") or "TikTok Buyer"
                ),
                country_code=recipient.get("region_code"),
            ),
            line_items=[
                line
                for item in order.get("line_items") or []
                if (line := self._normalize_line_item(item)) is not None
            ],
            channel_metadata=metadata,
            updated_at=datetime.fromtimestamp(int(order.get("update_time") or 0), UTC),
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        search = await self._signed_request(
            "POST",
            "/order/202309/orders/search",
            query={"page_size": "50"},
            body={
                "update_time_ge": int(since.timestamp()),
                "page_size": 50,
                **({"page_token": cursor} if cursor else {}),
            },
        )
        order_ids = [str(row["id"]) for row in search.get("orders") or [] if row.get("id")]
        next_token = str(search.get("next_page_token") or "")
        if not order_ids:
            return [], next_token or None
        details = await self._signed_request(
            "GET",
            "/order/202309/orders",
            query={"ids": ",".join(order_ids)},
        )
        orders = [
            self._normalize_order(row)
            for row in details.get("orders") or []
            if (row.get("line_items") or [])
        ]
        return orders, next_token or None

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        data = await self._signed_request(
            "POST",
            "/product/202309/products/search",
            query={"page_size": "50"},
            body={
                "update_time_ge": int(since.timestamp()),
                "page_size": 50,
                **({"page_token": cursor} if cursor else {}),
            },
        )
        products = [
            NormalizedProduct(
                external_product_id=str(row["id"]),
                title=str(row.get("title") or row["id"]),
                variants=list(row.get("skus") or []),
            )
            for row in data.get("products") or []
        ]
        next_token = str(data.get("next_page_token") or "")
        return products, next_token or None

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        if not fulfillment_order_id:
            raise AppError(
                slug="tiktok-package-required",
                status_code=422,
                code="ERR_TIKTOK_PACKAGE_REQUIRED",
                title="TikTok Package Required",
                detail="TikTok Shop fulfillment requires the package id.",
            )
        payload: dict[str, Any] = {
            "tracking_number": tracking_number,
            "carrier": carrier_company,
        }
        if tracking_url:
            payload["tracking_url"] = tracking_url
        return await self._signed_request(
            "POST",
            f"/fulfillment/202309/packages/{fulfillment_order_id}/ship",
            body=payload,
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_tiktok_webhook_signature(
            raw_body, headers, app_key=self.app_key, app_secret=secret or self.app_secret
        )


async def enforce_custom_app_shop_limit(session: AsyncSession, app_mode: str, app_key: str) -> None:
    """Public apps may serve many shops; seller BYOK custom apps are capped at 25 shops."""
    if app_mode != "seller_byok":
        return
    shops = await session.scalar(
        text(
            "SELECT count(DISTINCT external_account_id) FROM channel_accounts "
            "WHERE channel_type = 'tiktok_shop' AND config->>'app_key' = :app_key"
        ),
        {"app_key": app_key},
    )
    if int(shops or 0) >= TIKTOK_CUSTOM_APP_SHOP_LIMIT:
        raise AppError(
            slug="tiktok-custom-app-shop-limit",
            status_code=409,
            code="ERR_TIKTOK_CUSTOM_APP_SHOP_LIMIT",
            title="TikTok Custom App Shop Limit",
            detail="A seller BYOK custom app may connect at most 25 shops.",
        )
