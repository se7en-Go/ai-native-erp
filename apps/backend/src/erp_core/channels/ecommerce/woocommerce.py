"""WooCommerce REST v3 connector with meta-field mapping and order notes."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

WC_API_PREFIX = "/wp-json/wc/v3"


def build_wc_authorize_url(
    store_url: str,
    app_name: str,
    user_id: str,
    return_url: str,
    callback_url: str,
    scope: str = "read_write",
) -> str:
    parsed = urlparse(store_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AppError(
            slug="invalid-woocommerce-store",
            status_code=422,
            code="ERR_INVALID_WOOCOMMERCE_STORE",
            title="Invalid WooCommerce Store",
            detail="Store URL must be an absolute https URL.",
        )
    query = urlencode(
        {
            "app_name": app_name,
            "scope": scope,
            "user_id": user_id,
            "return_url": return_url,
            "callback_url": callback_url,
        }
    )
    return f"https://{parsed.netloc}/wc-auth/v1/authorize?{query}"


def verify_woocommerce_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    signature = headers.get("X-WC-Webhook-Signature") or headers.get("x-wc-webhook-signature") or ""
    if not signature or not secret:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature.strip())


class WooCommerceAdapter(CommercePlatformAdapter):
    """WooCommerce REST v3 adapter mapping merchant meta keys to B2B fields."""

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        meta_field_map: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        webhook_secret: str = "",
    ) -> None:
        parsed = urlparse(store_url)
        if not parsed.netloc:
            raise AppError(
                slug="invalid-woocommerce-store",
                status_code=422,
                code="ERR_INVALID_WOOCOMMERCE_STORE",
                title="Invalid WooCommerce Store",
                detail="Store URL must be absolute.",
            )
        self.store_url = f"https://{parsed.netloc}"
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.meta_field_map = dict(meta_field_map or {})
        self.webhook_secret = webhook_secret
        self._http_client = http_client

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = f"{self.store_url}{WC_API_PREFIX}{path}"
        auth = (self.consumer_key, self.consumer_secret)
        if self._http_client is not None:
            response = await self._http_client.request(
                method, url, params=params, json=json_body, auth=auth
            )
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, url, params=params, json=json_body, auth=auth
                )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _extract_meta(self, meta_list: list[dict[str, Any]]) -> dict[str, Any]:
        index = {str(item.get("key")): item.get("value") for item in meta_list}
        return {
            field: index[key]
            for field, key in self.meta_field_map.items()
            if key in index and index[key] not in (None, "")
        }

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value if value not in (None, "") else "0"))

    def _normalize_order(self, order: dict[str, Any]) -> NormalizedOrder:
        billing = order.get("billing") or {}
        meta = self._extract_meta(order.get("meta_data") or [])
        total = self._decimal(order.get("total"))
        shipping = self._decimal(order.get("shipping_total"))
        discount = self._decimal(order.get("discount_total"))
        tax = self._decimal(order.get("total_tax"))
        company = (billing.get("company") or "").strip()
        status = str(order.get("status") or "").lower()
        payment_status = (
            PaymentStatus.PAID
            if status in {"processing", "completed"}
            else PaymentStatus.REFUNDED
            if status == "refunded"
            else PaymentStatus.UNPAID
        )
        metadata: dict[str, Any] = {
            "wc_order_number": order.get("number"),
            "wc_status": status,
        }
        if "po_number" in meta:
            metadata["po_number"] = str(meta["po_number"])
        if "tracking_number" in meta:
            metadata["tracking_number"] = str(meta["tracking_number"])
        name = f"{billing.get('first_name') or ''} {billing.get('last_name') or ''}".strip()
        return NormalizedOrder(
            external_order_id=str(order["id"]),
            external_order_number=str(order.get("number") or order["id"]),
            order_mode=OrderMode.B2B_WHOLESALE if company else OrderMode.B2C_RETAIL,
            currency=str(order.get("currency") or "USD"),
            subtotal_amount=total - shipping - tax + discount,
            shipping_charged=shipping,
            discount_amount=discount,
            tax_collected=tax,
            total_amount=total,
            fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=billing.get("email"),
                phone_e164=billing.get("phone"),
                external_customer_id=str(order.get("customer_id") or "") or None,
                external_identity_type=IdentityType.EMAIL,
                display_name=name or billing.get("email") or "WooCommerce Buyer",
                country_code=billing.get("country"),
            ),
            b2b_company=(
                NormalizedB2BCompany(
                    legal_name=company, country_code=billing.get("country") or "US"
                )
                if company
                else None
            ),
            line_items=[
                NormalizedLineItem(
                    external_sku=str(item["sku"]),
                    title=str(item.get("name") or item["sku"]),
                    quantity=int(item["quantity"]),
                    line_total=self._decimal(item.get("total")),
                )
                for item in order.get("line_items") or []
                if str(item.get("sku") or "").strip()
            ],
            channel_metadata=metadata,
            updated_at=datetime.fromisoformat(
                str(order.get("date_modified_gmt") or order.get("date_created_gmt")).replace(
                    "Z", ""
                )
            ).replace(tzinfo=UTC),
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = int(cursor or 1)
        payload = await self._request(
            "GET",
            "/orders",
            params={
                "after": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "per_page": 50,
                "page": page,
                "orderby": "modified",
                "order": "asc",
            },
        )
        if not isinstance(payload, list):
            raise AppError(
                slug="woocommerce-orders-invalid",
                status_code=502,
                code="ERR_WOOCOMMERCE_ORDERS_INVALID",
                title="WooCommerce Orders Invalid",
                detail="Expected a list of orders.",
            )
        orders = [self._normalize_order(row) for row in payload]
        return orders, str(page + 1) if len(payload) == 50 else None

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = int(cursor or 1)
        payload = await self._request(
            "GET",
            "/products",
            params={
                "modified_after": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "per_page": 50,
                "page": page,
            },
        )
        rows = payload if isinstance(payload, list) else []
        products = [
            NormalizedProduct(
                external_product_id=str(row["id"]),
                title=str(row.get("name") or row["id"]),
                variants=list(row.get("variations") or []),
            )
            for row in rows
        ]
        return products, str(page + 1) if len(rows) == 50 else None

    async def post_order_note(
        self, external_order_id: str, note: str, customer_note: bool = True
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            f"/orders/{external_order_id}/notes",
            json_body={"note": note, "customer_note": customer_note},
        )
        return payload if isinstance(payload, dict) else {}

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        note = f"Shipped via {carrier_company}. Tracking number: {tracking_number}"
        if tracking_url:
            note = f"{note} ({tracking_url})"
        await self.post_order_note(external_order_id, note, customer_note=notify_customer)
        result = await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments",
            json_body={
                "tracking_number": tracking_number,
                "carrier": carrier_company,
                "notify_customer": notify_customer,
            },
        )
        return result if isinstance(result, dict) else {}

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_woocommerce_webhook(raw_body, headers, secret or self.webhook_secret)
