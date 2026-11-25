"""TikTok Shop Core connector: signed Open API calls, BYOK custom apps, webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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

TIKTOK_API_BASE = "https://open-api.tiktokglobalshop.com"
TIKTOK_CUSTOM_APP_SHOP_LIMIT = 25
_UNSIGNED_PARAMS = frozenset({"sign", "access_token"})
_STATUS_MAP = {
    "UNPAID": FulfillmentStatus.PENDING_PAYMENT,
    "ON_HOLD": FulfillmentStatus.CONFIRMED_RESERVED,
    "AWAITING_SHIPMENT": FulfillmentStatus.CONFIRMED_RESERVED,
    "AWAITING_COLLECTION": FulfillmentStatus.CONFIRMED_RESERVED,
    "PARTIALLY_SHIPPING": FulfillmentStatus.PARTIALLY_SHIPPED,
    "IN_TRANSIT": FulfillmentStatus.SHIPPED,
    "DELIVERED": FulfillmentStatus.DELIVERED,
    "COMPLETED": FulfillmentStatus.DELIVERED,
    "CANCELLED": FulfillmentStatus.CANCELLED,
    "CANCELED": FulfillmentStatus.CANCELLED,
    "REFUNDED": FulfillmentStatus.REFUNDED,
    "RETURNED": FulfillmentStatus.REFUNDED,
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


class TikTokListingReceipt(BaseModel):
    external_product_id: str
    external_sku: str
    status: str = "ACTIVATE"
    variant_receipts: list[dict[str, Any]] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


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
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = TIKTOK_API_BASE,
        webhook_secret: str = "",
        warehouse_id: str | None = None,
        category_id: str = "601452",
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.shop_cipher = shop_cipher
        self.app_mode = app_mode
        self._http_client = http_client
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.warehouse_id = warehouse_id
        self.default_category_id = category_id

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
            async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
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
        status = str(order.get("status") or "").strip().upper()
        cancelled_at = order.get("cancel_time") or order.get("cancelled_at")
        metadata: dict[str, Any] = {
            "tiktok_order_status": status,
            "tiktok_user_id": order.get("user_id"),
        }
        if cancelled_at:
            metadata["cancelled_at"] = str(cancelled_at)
        if order.get("packages"):
            metadata["tiktok_package_id"] = (order["packages"][0] or {}).get("id")
        total = self._decimal(payment.get("total_amount"))
        refunded_val = self._decimal(
            order.get("refund_amount") or payment.get("seller_refund_amount")
        ).copy_abs()
        commission_val = self._decimal(
            payment.get("platform_commission") or payment.get("referral_fee")
        )
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded_val,
            platform_commission_base=commission_val,
        )
        is_cancelled = status in {"CANCELLED", "CANCELED"} or bool(cancelled_at)
        is_full_refund = status in {"REFUNDED", "RETURNED", "FULLY_REFUNDED"} or (
            total > Decimal("0") and refunded_val >= total
        )
        declared_payment = str(
            order.get("payment_status") or payment.get("payment_status") or ""
        ).strip().upper()
        if is_cancelled:
            fulfillment_status = FulfillmentStatus.CANCELLED
            payment_status = (
                PaymentStatus.UNPAID
                if declared_payment == "UNPAID" and refunded_val == Decimal("0")
                else PaymentStatus.REFUNDED
            )
        elif is_full_refund:
            fulfillment_status = FulfillmentStatus.REFUNDED
            payment_status = PaymentStatus.REFUNDED
        elif status == "UNPAID" or declared_payment == "UNPAID":
            fulfillment_status = FulfillmentStatus.PENDING_PAYMENT
            payment_status = PaymentStatus.UNPAID
        elif status == "PARTIALLY_PAID" or declared_payment == "PARTIALLY_PAID":
            fulfillment_status = _STATUS_MAP.get(status, FulfillmentStatus.PENDING_PAYMENT)
            payment_status = PaymentStatus.PARTIALLY_PAID
        else:
            fulfillment_status = _STATUS_MAP.get(status, FulfillmentStatus.CONFIRMED_RESERVED)
            payment_status = PaymentStatus.PAID
        return NormalizedOrder(
            external_order_id=str(order["id"]),
            external_order_number=str(order.get("id")),
            order_mode=OrderMode.B2C_RETAIL,
            currency=str(payment.get("currency") or "USD"),
            subtotal_amount=self._decimal(payment.get("sub_total")),
            shipping_charged=self._decimal(payment.get("shipping_fee")),
            discount_amount=self._decimal(payment.get("platform_discount")),
            tax_collected=self._decimal(payment.get("tax")),
            total_amount=total,
            platform_commission_base=commission_val.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
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
        del notify_customer
        package_id = (fulfillment_order_id or "").strip() or None
        if not package_id and external_order_id.strip():
            details = await self._signed_request(
                "GET",
                "/order/202309/orders",
                query={"ids": external_order_id.strip()},
            )
            for order_row in details.get("orders") or []:
                if not isinstance(order_row, Mapping):
                    continue
                for pkg in order_row.get("packages") or []:
                    if isinstance(pkg, Mapping) and pkg.get("id"):
                        package_id = str(pkg["id"]).strip()
                        break
                if package_id:
                    break
                for li in order_row.get("line_items") or []:
                    if isinstance(li, Mapping) and li.get("package_id"):
                        package_id = str(li["package_id"]).strip()
                        break
                if package_id:
                    break
        if not package_id:
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
        data = await self._signed_request(
            "POST",
            f"/fulfillment/202309/packages/{package_id}/ship",
            body=payload,
        )
        return {
            "fulfillment_id": package_id,
            "package_id": package_id,
            "raw": data,
        }

    async def _upload_image_blob(self, blob: Mapping[str, Any]) -> str | None:
        raw = blob.get("data")
        if not raw:
            return None
        path = "/product/202309/images/upload"
        params = {
            "app_key": self.app_key,
            "shop_cipher": self.shop_cipher,
            "timestamp": str(int(time.time())),
        }
        params["sign"] = sign_tiktok_request(self.app_secret, path, params, "")
        url = f"{self.base_url}{path}?{urlencode(params)}"
        headers = {"x-tts-access-token": self.access_token}
        fname = str(blob.get("filename") or "product.jpg")
        mime = str(blob.get("mime_type") or "image/jpeg")
        files = {"data": (fname, bytes(raw), mime)}
        data_fields = {"use_case": "MAIN_IMAGE"}
        if self._http_client is not None:
            resp = await self._http_client.post(url, data=data_fields, files=files, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
                resp = await client.post(url, data=data_fields, files=files, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        if int(payload.get("code", 0)) != 0:
            raise AppError(
                slug="tiktok-media-upload-failed",
                status_code=502,
                code="ERR_TIKTOK_SHOP_API",
                title="TikTok Image Upload Failed",
                detail=json.dumps(payload)[:400],
            )
        uri = ((payload.get("data") or {}).get("uri")) or ""
        return str(uri) if uri else None

    async def _resolve_warehouse_id(self, warehouse_id: str | None = None) -> str | None:
        wh_id = (warehouse_id or "").strip() or (self.warehouse_id or "").strip() or None
        if wh_id:
            return wh_id
        wh_data = await self._signed_request("GET", "/logistics/202309/warehouses")
        wh_rows = wh_data.get("warehouses") or wh_data.get("warehouse_list") or []
        if wh_rows and isinstance(wh_rows[0], Mapping) and wh_rows[0].get("id"):
            wh_id = str(wh_rows[0]["id"]).strip()
            self.warehouse_id = wh_id
        return wh_id

    async def publish_listing(
        self,
        *,
        sku: str,
        title: str,
        description: str,
        bullet_points: Sequence[str],
        price_usd: Decimal,
        quantity: int = 0,
        images: Sequence[str] | None = None,
        category: str = "",
        hs_code: str | None = None,
        moq_tiers: Sequence[Mapping[str, Any]] | None = None,
        options: Sequence[Mapping[str, Any]] | None = None,
        variants: Sequence[Mapping[str, Any]] | None = None,
        image_blobs: Sequence[Mapping[str, Any]] | None = None,
    ) -> TikTokListingReceipt:
        """Create one single- or multi-SKU listing via TikTok Shop ``/product/202309/products``."""
        del category, hs_code, moq_tiers, options
        clean_sku = sku.strip()
        clean_title = title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="tiktok-listing-rejected",
                status_code=422,
                code="ERR_TIKTOK_LISTING_REJECTED",
                title="TikTok Listing Rejected",
                detail="TikTok Shop listing requires a non-empty SKU and title.",
            )
        main_images: list[dict[str, str]] = []
        for blob in image_blobs or ():
            uri = await self._upload_image_blob(blob)
            if uri:
                main_images.append({"uri": uri})
        for url in images or ():
            clean_url = (url or "").strip()
            if clean_url.startswith("https://"):
                main_images.append({"uri": clean_url})

        desc_lines = [description.strip(), *(f"• {b.strip()}" for b in bullet_points if b.strip())]
        full_description = "\n".join(line for line in desc_lines if line) or clean_title
        wh_id = await self._resolve_warehouse_id()
        if not wh_id:
            raise AppError(
                slug="tiktok-listing-rejected",
                status_code=502,
                code="ERR_TIKTOK_LISTING_REJECTED",
                title="TikTok Listing Rejected",
                detail=(
                    "No TikTok Shop warehouse_id configured or returned by "
                    "/logistics/202309/warehouses."
                ),
            )

        variant_list = list(variants or ())
        skus_payload: list[dict[str, Any]] = []
        if variant_list:
            for var in variant_list:
                v_sku = str(var.get("external_sku") or clean_sku).strip()
                v_price = Decimal(str(var.get("price_usd") or price_usd)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                raw_qty = var.get("quantity")
                v_qty = max(0, int(raw_qty if raw_qty is not None else quantity))
                attrs = [
                    {"name": str(k), "value_name": str(v)}
                    for k, v in (var.get("option_values") or {}).items()
                ]
                skus_payload.append(
                    {
                        "seller_sku": v_sku,
                        "price": {"amount": str(v_price), "currency": "USD"},
                        "inventory": [{"warehouse_id": wh_id, "quantity": v_qty}],
                        **({"sales_attributes": attrs} if attrs else {}),
                    }
                )
        else:
            norm_price = Decimal(str(price_usd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            skus_payload.append(
                {
                    "seller_sku": clean_sku,
                    "price": {"amount": str(norm_price), "currency": "USD"},
                    "inventory": [{"warehouse_id": wh_id, "quantity": max(0, int(quantity))}],
                }
            )

        body: dict[str, Any] = {
            "title": clean_title[:255],
            "description": full_description[:10000],
            "category_id": self.default_category_id,
            "skus": skus_payload,
            "package_weight": {"value": "0.5", "unit": "KILOGRAM"},
        }
        if main_images:
            body["main_images"] = main_images[:9]

        data = await self._signed_request("POST", "/product/202309/products", body=body)
        product_id = str(data.get("product_id") or "").strip()
        if not product_id:
            raise AppError(
                slug="tiktok-listing-rejected",
                status_code=502,
                code="ERR_TIKTOK_LISTING_REJECTED",
                title="TikTok Listing Rejected",
                detail=f"TikTok Shop returned no product_id for '{clean_sku}'.",
            )
        variant_receipts = [
            {
                "external_sku": str(item.get("seller_sku") or clean_sku),
                "external_variant_id": str(item.get("id") or "") or None,
                "external_inventory_item_id": wh_id,
            }
            for item in (data.get("skus") or [])
            if isinstance(item, Mapping)
        ]
        return TikTokListingReceipt(
            external_product_id=product_id,
            external_sku=clean_sku,
            status="ACTIVATE",
            variant_receipts=variant_receipts,
            raw_response=data,
        )

    async def push_inventory_level(
        self,
        *,
        external_sku: str,
        quantity: int,
        external_product_id: str | None = None,
        external_variant_id: str | None = None,
        warehouse_id: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        """Update stock for one TikTok Shop SKU via inventory update endpoint."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="tiktok-inventory-sync-failed",
                status_code=422,
                code="ERR_TIKTOK_INVENTORY_SYNC",
                title="TikTok Shop Inventory Sync Failed",
                detail="TikTok Shop inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        wh_id = await self._resolve_warehouse_id(warehouse_id)
        if not wh_id:
            raise AppError(
                slug="tiktok-inventory-sync-failed",
                status_code=502,
                code="ERR_TIKTOK_INVENTORY_SYNC",
                title="TikTok Shop Inventory Sync Failed",
                detail=(
                    "No TikTok Shop warehouse_id configured or returned by "
                    "/logistics/202309/warehouses."
                ),
            )

        prod_id = (external_product_id or "").strip() or None
        sku_id = (external_variant_id or "").strip() or None
        if not prod_id or not sku_id:
            search_data = await self._signed_request(
                "POST",
                "/product/202309/products/search",
                query={"page_size": "20"},
                body={"seller_skus": [clean_sku], "page_size": 20},
            )
            for prod in search_data.get("products") or []:
                if not isinstance(prod, Mapping):
                    continue
                if prod_id and str(prod.get("id") or "") != prod_id:
                    continue
                for s in prod.get("skus") or []:
                    if (
                        isinstance(s, Mapping)
                        and str(s.get("seller_sku") or "").strip() == clean_sku
                    ):
                        prod_id = prod_id or str(prod.get("id") or "").strip() or None
                        sku_id = sku_id or str(s.get("id") or "").strip() or None
                        break
                if prod_id and sku_id:
                    break
        if not prod_id:
            raise AppError(
                slug="tiktok-inventory-sync-failed",
                status_code=502,
                code="ERR_TIKTOK_INVENTORY_SYNC",
                title="TikTok Shop Inventory Sync Failed",
                detail=f"Could not resolve TikTok product_id for SKU '{clean_sku}'.",
            )
        target_sku_id = sku_id or clean_sku
        data = await self._signed_request(
            "POST",
            f"/product/202309/products/{prod_id}/inventory/update",
            body={
                "skus": [
                    {
                        "id": target_sku_id,
                        "inventory": [{"warehouse_id": wh_id, "quantity": norm_qty}],
                    }
                ]
            },
        )
        return {
            "channel": "tiktok_shop",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "product_id": prod_id,
            "variant_id": target_sku_id,
            "warehouse_id": wh_id,
            "raw": data,
        }

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
