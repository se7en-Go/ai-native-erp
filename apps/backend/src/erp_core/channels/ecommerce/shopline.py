"""SHOPLINE connector with Redis-backed leaky-bucket pacing and B2B PO mapping."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
    attach_financial_metadata,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

SHOPLINE_API_VERSION = "v20240301"
_CONSUME_TOKENS = """
local rate = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or tonumber(ARGV[3])
local ts = tonumber(state[2]) or now
tokens = math.min(tonumber(ARGV[3]), tokens + math.max(0, now - ts) * rate)
local allowed = 0
local wait_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait_ms = math.ceil((1 - tokens) / rate * 1000)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 60000)
return {allowed, wait_ms}
"""


class LeakyBucketLimiter:
    """Shared leaky bucket: Redis Lua for workers, in-process fallback for local tests."""

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client
        self._local: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, bucket_key: str, rate_per_sec: float, capacity: int
    ) -> tuple[bool, int]:
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate_per_sec and capacity must be positive")
        now = time.monotonic()
        if self._redis is not None:
            allowed, wait_ms = await self._redis.eval(
                _CONSUME_TOKENS,
                1,
                f"erp:leaky:{bucket_key}",
                str(rate_per_sec),
                str(time.time()),
                str(capacity),
            )
            return bool(int(allowed)), int(wait_ms)
        async with self._lock:
            tokens, stamp = self._local.get(bucket_key, (float(capacity), now))
            tokens = min(float(capacity), tokens + max(0.0, now - stamp) * rate_per_sec)
            if tokens >= 1:
                self._local[bucket_key] = (tokens - 1, now)
                return True, 0
            self._local[bucket_key] = (tokens, now)
            return False, int((1 - tokens) / rate_per_sec * 1000)

    async def wait_and_acquire(self, bucket_key: str, rate_per_sec: float, capacity: int) -> None:
        allowed, wait_ms = await self.acquire(bucket_key, rate_per_sec, capacity)
        while not allowed:
            await asyncio.sleep(max(wait_ms, 1) / 1000)
            allowed, wait_ms = await self.acquire(bucket_key, rate_per_sec, capacity)


def verify_shopline_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    signature = headers.get("X-Shopline-Hmac-Sha256") or headers.get("x-shopline-hmac-sha256") or ""
    if not signature or not secret:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature.strip())


class ShoplineListingReceipt(BaseModel):
    external_product_id: str
    external_sku: str
    status: str = "active"
    variant_receipts: list[dict[str, Any]] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ShoplineAdapter(CommercePlatformAdapter):
    """SHOPLINE OpenAPI adapter (B2B purchase orders + leaky-bucket pacing)."""

    def __init__(
        self,
        store_domain: str,
        access_token: str,
        leaky_bucket: LeakyBucketLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        webhook_secret: str = "",
        rate_per_sec: float = 4.0,
        capacity: int = 40,
    ) -> None:
        self.store_domain = store_domain.strip().lower()
        self.access_token = access_token
        self.bucket = leaky_bucket or LeakyBucketLimiter()
        self._http_client = http_client
        self._transport = transport
        self.webhook_secret = webhook_secret
        self.rate_per_sec = rate_per_sec
        self.capacity = capacity

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        await self.bucket.wait_and_acquire(
            f"shopline:{self.store_domain}", self.rate_per_sec, self.capacity
        )
        url = f"https://{self.store_domain}/admin/openapi/{SHOPLINE_API_VERSION}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if self._http_client is not None:
            response = await self._http_client.request(
                method, url, params=params, json=json_body, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AppError(
                slug="shopline-invalid-response",
                status_code=502,
                code="ERR_SHOPLINE_INVALID_RESPONSE",
                title="SHOPLINE Invalid Response",
                detail="Expected a JSON object.",
            )
        return payload

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value if value not in (None, "") else "0"))

    def _normalize_order(self, order: dict[str, Any]) -> NormalizedOrder:
        customer = order.get("customer") or {}
        location = order.get("b2b_company_location") or {}
        company_name = str(location.get("company_name") or "").strip()
        cancelled_at = order.get("cancelled_at") or order.get("cancel_time")
        status_raw = str(order.get("status") or "").strip().lower()
        is_cancelled = bool(cancelled_at) or status_raw in {"cancelled", "canceled"}
        financial = str(order.get("financial_status") or "").strip().lower()
        fulfillment = str(order.get("fulfillment_status") or "").strip().lower()
        total = self._decimal(order.get("total_price"))
        metadata: dict[str, Any] = {
            "shopline_order_number": order.get("order_number"),
            "financial_status": financial or None,
            "fulfillment_status": fulfillment or None,
        }
        if cancelled_at:
            metadata["cancelled_at"] = str(cancelled_at)
        if order.get("purchase_order_number"):
            metadata["po_number"] = order["purchase_order_number"]
        refunded_val = self._decimal(
            order.get("total_refunded") or order.get("refunded_amount")
        ).copy_abs()
        commission_val = self._decimal(
            order.get("platform_commission") or order.get("platform_fee")
        )
        attach_financial_metadata(
            metadata,
            refunded_amount=refunded_val,
            platform_commission_base=commission_val,
        )
        is_full_refund = financial in {"refunded", "fully_refunded"} or (
            total > Decimal("0") and refunded_val >= total
        )
        if is_cancelled:
            payment_status = (
                PaymentStatus.REFUNDED
                if (
                    financial
                    in {
                        "paid",
                        "partially_paid",
                        "partially_refunded",
                        "refunded",
                        "fully_refunded",
                    }
                    or refunded_val > Decimal("0")
                )
                else PaymentStatus.UNPAID
            )
            fulfillment_status = FulfillmentStatus.CANCELLED
        elif is_full_refund:
            payment_status = PaymentStatus.REFUNDED
            fulfillment_status = FulfillmentStatus.REFUNDED
        else:
            if financial in {"paid", "partially_refunded"} or (
                Decimal("0") < refunded_val < total
            ):
                payment_status = PaymentStatus.PAID
            elif financial == "partially_paid":
                payment_status = PaymentStatus.PARTIALLY_PAID
            else:
                payment_status = PaymentStatus.UNPAID

            if fulfillment in {"fulfilled", "shipped"}:
                fulfillment_status = FulfillmentStatus.SHIPPED
            elif fulfillment == "delivered":
                fulfillment_status = FulfillmentStatus.DELIVERED
            elif fulfillment in {"partial", "partially_fulfilled", "partially_shipped"}:
                fulfillment_status = FulfillmentStatus.PARTIALLY_SHIPPED
            elif payment_status == PaymentStatus.UNPAID and financial in {
                "unpaid",
                "pending",
                "authorized",
                "voided",
            }:
                fulfillment_status = FulfillmentStatus.PENDING_PAYMENT
            else:
                fulfillment_status = FulfillmentStatus.CONFIRMED_RESERVED
        name = f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}".strip()
        return NormalizedOrder(
            external_order_id=str(order["id"]),
            external_order_number=str(order.get("order_number") or order["id"]),
            order_mode=OrderMode.B2B_WHOLESALE if company_name else OrderMode.B2C_RETAIL,
            currency=str(order.get("currency") or "USD"),
            subtotal_amount=self._decimal(order.get("subtotal_price")),
            shipping_charged=self._decimal(order.get("total_shipping_price")),
            tax_collected=self._decimal(order.get("total_tax")),
            total_amount=self._decimal(order.get("total_price")),
            platform_commission_base=commission_val.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=customer.get("email"),
                phone_e164=customer.get("phone"),
                external_customer_id=str(customer.get("id") or "") or None,
                external_identity_type=IdentityType.EMAIL,
                display_name=name or customer.get("email") or "SHOPLINE Buyer",
                country_code=location.get("country_code"),
            ),
            b2b_company=(
                NormalizedB2BCompany(
                    legal_name=company_name, country_code=location.get("country_code") or "US"
                )
                if company_name
                else None
            ),
            line_items=[
                NormalizedLineItem(
                    external_sku=str(item["sku"]),
                    title=str(item.get("title") or item["sku"]),
                    quantity=int(item["quantity"]),
                    line_total=self._decimal(item.get("price")) * int(item["quantity"]),
                )
                for item in order.get("line_items") or []
                if str(item.get("sku") or "").strip()
            ],
            channel_metadata=metadata,
            updated_at=datetime.fromisoformat(
                str(order["updated_at"]).replace("Z", "+00:00")
            ).astimezone(UTC),
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        payload = await self._request(
            "GET",
            "/orders.json",
            params={
                "updated_at_min": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 50,
                "page": int(cursor or 1),
            },
        )
        rows = payload.get("orders") or []
        return [
            self._normalize_order(row)
            for row in rows
            if any(str(item.get("sku") or "").strip() for item in (row.get("line_items") or []))
        ], (str(int(cursor or 1) + 1) if len(rows) == 50 else None)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        payload = await self._request(
            "GET",
            "/products.json",
            params={
                "updated_at_min": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 50,
                "page": int(cursor or 1),
            },
        )
        rows = payload.get("products") or []
        return [
            NormalizedProduct(
                external_product_id=str(row["id"]),
                title=str(row.get("title") or row["id"]),
                variants=list(row.get("variants") or []),
            )
            for row in rows
        ], (str(int(cursor or 1) + 1) if len(rows) == 50 else None)

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        del fulfillment_order_id
        body: dict[str, Any] = {
            "tracking_number": tracking_number,
            "tracking_company": carrier_company,
            "notify_customer": notify_customer,
        }
        if tracking_url:
            body["tracking_url"] = tracking_url
        res = await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments.json",
            json_body=body,
        )
        data = dict(res)
        fid = data.get("fulfillment_id") or data.get("id")
        if not fid and isinstance(data.get("fulfillment"), Mapping):
            fid = data["fulfillment"].get("id")
        if not fid and isinstance(data.get("data"), Mapping):
            fid = data["data"].get("id")
        if fid:
            data["fulfillment_id"] = str(fid)
        return data

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
    ) -> ShoplineListingReceipt:
        """Create a single- or multi-variant listing through SHOPLINE OpenAPI."""
        del hs_code, moq_tiers, image_blobs
        clean_sku = sku.strip()
        clean_title = title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="shopline-listing-rejected",
                status_code=422,
                code="ERR_SHOPLINE_LISTING_REJECTED",
                title="SHOPLINE Listing Rejected",
                detail="SHOPLINE listing requires a non-empty SKU and title.",
            )
        norm_price = Decimal(str(price_usd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        desc_parts = [
            f"<p>{html.escape(line.strip())}</p>"
            for line in description.splitlines()
            if line.strip()
        ]
        if bullet_points:
            items = "".join(
                f"<li>{html.escape(b.strip())}</li>" for b in bullet_points if b.strip()
            )
            if items:
                desc_parts.append(f"<ul>{items}</ul>")

        variant_list = list(variants or ())
        sl_variants: list[dict[str, Any]] = []
        synthesized_options: dict[str, list[str]] = {}
        if variant_list:
            for var in variant_list:
                opt_map = var.get("option_values") or {}
                opt_vals = list(opt_map.values())
                for k, v in opt_map.items():
                    k_str = str(k).strip()
                    v_str = str(v).strip()
                    if k_str and v_str:
                        vals = synthesized_options.setdefault(k_str, [])
                        if v_str not in vals:
                            vals.append(v_str)
                v_entry: dict[str, Any] = {
                    "sku": str(var.get("external_sku") or clean_sku),
                    "price": str(
                        Decimal(str(var.get("price_usd") or norm_price)).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    ),
                    "inventory_quantity": max(
                        0,
                        int(var["quantity"]) if var.get("quantity") is not None else quantity,
                    ),
                    "inventory_tracker": True,
                }
                for idx, val in enumerate(opt_vals[:3], start=1):
                    v_entry[f"option{idx}"] = str(val)
                sl_variants.append(v_entry)
        else:
            sl_variants.append(
                {
                    "sku": clean_sku,
                    "price": str(norm_price),
                    "inventory_quantity": max(0, int(quantity)),
                    "inventory_tracker": True,
                    "option1": "Default Title",
                }
            )
            synthesized_options["Title"] = ["Default Title"]
        sl_options = [
            {"name": str(opt["name"]), "values": [str(v) for v in opt.get("values") or ()]}
            for opt in (options or ())
            if opt.get("name")
        ]
        if not sl_options and synthesized_options:
            sl_options = [
                {"name": name, "values": vals} for name, vals in synthesized_options.items()
            ]
        product_body: dict[str, Any] = {
            "title": clean_title,
            "body_html": "".join(desc_parts) or f"<p>{html.escape(clean_title)}</p>",
            "product_type": category or "General",
            "status": "active",
            "variants": sl_variants,
        }
        if sl_options:
            product_body["options"] = sl_options
        img_list = [
            {"src": url.strip()}
            for url in (images or ())
            if url and url.strip().startswith("https://")
        ]
        if img_list:
            product_body["images"] = img_list[:15]

        res = await self._request(
            "POST", "/products/products.json", json_body={"product": product_body}
        )
        prod = res.get("product") if isinstance(res.get("product"), Mapping) else res
        product_id = str((prod or {}).get("id") or "").strip()
        if not product_id:
            raise AppError(
                slug="shopline-listing-rejected",
                status_code=502,
                code="ERR_SHOPLINE_LISTING_REJECTED",
                title="SHOPLINE Listing Rejected",
                detail=f"SHOPLINE returned no product id for '{clean_sku}'.",
            )
        variant_receipts = [
            {
                "external_sku": str(v.get("sku") or clean_sku),
                "external_variant_id": str(v.get("id") or "") or None,
                "external_inventory_item_id": (
                    str(v.get("inventory_item_id") or "") or None
                ),
            }
            for v in (prod or {}).get("variants") or []
            if isinstance(v, Mapping)
        ]
        return ShoplineListingReceipt(
            external_product_id=product_id,
            external_sku=clean_sku,
            status="active",
            variant_receipts=variant_receipts,
            raw_response=dict(res),
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
        **_extra: Any,
    ) -> dict[str, Any]:
        """Set SHOPLINE inventory level via ``/inventory_levels/set.json``."""
        del external_variant_id
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="shopline-inventory-sync-failed",
                status_code=422,
                code="ERR_SHOPLINE_INVENTORY_SYNC",
                title="SHOPLINE Inventory Sync Failed",
                detail="SHOPLINE inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        item_id = (external_inventory_item_id or "").strip() or None
        if not item_id and external_product_id:
            prod_res = await self._request("GET", f"/products/{external_product_id.strip()}.json")
            prod = (
                prod_res.get("product")
                if isinstance(prod_res.get("product"), Mapping)
                else prod_res
            )
            for var in (prod or {}).get("variants") or []:
                if isinstance(var, Mapping) and str(var.get("sku") or "").strip() == clean_sku:
                    item_id = str(var.get("inventory_item_id") or "").strip() or None
                    break
        if not item_id:
            raise AppError(
                slug="shopline-inventory-sync-failed",
                status_code=502,
                code="ERR_SHOPLINE_INVENTORY_SYNC",
                title="SHOPLINE Inventory Sync Failed",
                detail=f"Could not resolve SHOPLINE inventory_item_id for SKU '{clean_sku}'.",
            )
        loc_id = (location_id or "").strip() or None
        if not loc_id:
            loc_res = await self._request("GET", "/locations.json")
            locs = loc_res.get("locations") or []
            if locs and isinstance(locs[0], Mapping) and locs[0].get("id"):
                loc_id = str(locs[0]["id"]).strip()
        body: dict[str, Any] = {
            "inventory_item_id": item_id,
            "available": norm_qty,
        }
        if loc_id:
            body["location_id"] = loc_id
        res = await self._request("POST", "/inventory_levels/set.json", json_body=body)
        return {
            "channel": "shopline",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_inventory_item_id": item_id,
            "location_id": loc_id,
            "raw": res,
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shopline_webhook(raw_body, headers, secret or self.webhook_secret)
