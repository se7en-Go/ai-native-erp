"""WooCommerce REST v3 connector with meta-field mapping and order notes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import os
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from pydantic import BaseModel, Field

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

_DEFAULT_STORE_SCHEME = "https"
_ALLOW_LOCAL_ENV = "ALLOW_LOCAL_WEBHOOK_URLS"
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "::1"})
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet")
# RFC 6598 carrier-grade NAT space: not ``is_private`` on every CPython release.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

DnsResolver = Callable[[str], Awaitable[Sequence[str]]]


def _invalid_store(detail: str) -> AppError:
    return AppError(
        slug="invalid-woocommerce-store",
        status_code=422,
        code="ERR_INVALID_WOOCOMMERCE_STORE",
        title="Invalid WooCommerce Store",
        detail=detail,
    )


def _unsafe_store(detail: str) -> AppError:
    return AppError(
        slug="unsafe-woocommerce-store",
        status_code=422,
        code="ERR_WOOCOMMERCE_UNSAFE_HOST",
        title="Unsafe WooCommerce Store Host",
        detail=detail,
    )


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Fail closed: only globally routable unicast addresses are acceptable."""
    mapped = getattr(value, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_ip(mapped)
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
        or (isinstance(value, ipaddress.IPv4Address) and value in _SHARED_ADDRESS_SPACE)
    )


def allow_local_store_hosts() -> bool:
    """Explicit dev/test override for loopback store URLs (``ALLOW_LOCAL_WEBHOOK_URLS``)."""
    raw = os.environ.get(_ALLOW_LOCAL_ENV, "").strip().lower()
    return bool(raw) and raw not in {"0", "false", "no", "off"}


async def system_dns_resolver(host: str) -> Sequence[str]:
    """Resolve ``host`` through the platform stub resolver (``()`` when unknown)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return ()
    return tuple({str(info[4][0]) for info in infos})


def normalize_store_url(store_url: str, *, allow_local: bool = False) -> str:
    """Normalise a store URL to an absolute ``https`` base URL and refuse unsafe hosts.

    Accepts bare domains (``shop.example.com`` -> ``https://shop.example.com``),
    rejects non-http(s) schemes, embedded credentials, internal hostnames and
    private/loopback/link-local/reserved/CGNAT IP literals. No DNS is performed
    here — see :func:`validate_store_url` for the network check.
    """
    candidate = (store_url or "").strip()
    if not candidate:
        raise _invalid_store("Store URL must not be empty.")
    if "://" not in candidate:
        candidate = f"{_DEFAULT_STORE_SCHEME}://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise _invalid_store("Store URL must be an absolute http(s) URL.")
    if parsed.username or parsed.password:
        raise _invalid_store("Store URL must not embed credentials.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise _invalid_store("Store URL must include a hostname.")
    if not allow_local:
        if host in _LOCAL_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
            raise _unsafe_store(f"Host '{host}' is a local or internal target.")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not _is_public_ip(literal):
            raise _unsafe_store(f"IP literal '{host}' is not a public internet address.")
    # Ports survive (``netloc`` includes them); the scheme is always normalised to https.
    return f"{_DEFAULT_STORE_SCHEME}://{parsed.netloc}"


async def validate_store_url(
    store_url: str,
    *,
    resolve_dns: bool = True,
    dns_resolver: DnsResolver | None = None,
    allow_local: bool | None = None,
) -> str:
    """Normalise + validate a WooCommerce store URL, optionally re-checking DNS answers.

    ``resolve_dns`` must be ``True`` for every real outbound API call; unit tests that
    inject a mocked ``httpx`` client/transport pass ``False`` because their hostnames
    are intentionally fake.
    """
    local_allowed = allow_local_store_hosts() if allow_local is None else allow_local
    normalized = normalize_store_url(store_url, allow_local=local_allowed)
    if not resolve_dns or local_allowed:
        return normalized
    host = (urlparse(normalized).hostname or "").strip().lower().rstrip(".")
    resolver = dns_resolver or system_dns_resolver
    try:
        answers = await resolver(host)
    except OSError as exc:
        raise _unsafe_store(
            f"DNS resolution for '{host}' failed ({type(exc).__name__})."
        ) from exc
    addresses = sorted(
        {str(answer).strip().strip("[]") for answer in answers if str(answer).strip()}
    )
    if not addresses:
        raise _unsafe_store(f"Host '{host}' did not resolve to any address.")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise _unsafe_store(
                f"Host '{host}' resolved to an unparsable address '{address}'."
            ) from exc
        if not _is_public_ip(resolved):
            raise _unsafe_store(
                f"Host '{host}' resolves to non-public address '{address}'."
            )
    return normalized


def build_wc_authorize_url(
    store_url: str,
    app_name: str,
    user_id: str,
    return_url: str,
    callback_url: str,
    scope: str = "read_write",
) -> str:
    parsed = urlparse(normalize_store_url(store_url))
    if parsed.scheme != "https" or not parsed.netloc:
        raise _invalid_store("Store URL must be an absolute https URL.")
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


class WooListingReceipt(BaseModel):
    external_product_id: str
    external_sku: str
    status: str = "publish"
    variant_receipts: list[dict[str, Any]] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


class WooCommerceAdapter(CommercePlatformAdapter):
    """WooCommerce REST v3 adapter mapping merchant meta keys to B2B fields."""

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        meta_field_map: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        webhook_secret: str = "",
    ) -> None:
        self._allow_local = allow_local_store_hosts()
        # A real deployment gets a DNS re-check before every outbound request; a mock
        # client/transport injected by a test already owns transport, so skip it.
        self._resolve_dns = http_client is None and transport is None
        self.store_url = normalize_store_url(store_url, allow_local=self._allow_local)
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.meta_field_map = dict(meta_field_map or {})
        self.webhook_secret = webhook_secret
        self._http_client = http_client
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        if self._resolve_dns:
            await validate_store_url(self.store_url, allow_local=self._allow_local)
        url = f"{self.store_url}{WC_API_PREFIX}{path}"
        auth = (self.consumer_key, self.consumer_secret)
        if self._http_client is not None:
            response = await self._http_client.request(
                method, url, params=params, json=json_body, auth=auth
            )
        else:
            async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
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
    ) -> WooListingReceipt:
        """Create one ``simple`` or ``variable`` product (plus batch variations) via REST v3."""
        del category, hs_code, moq_tiers, image_blobs
        clean_sku = sku.strip()
        clean_title = title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="woocommerce-listing-rejected",
                status_code=422,
                code="ERR_WOOCOMMERCE_LISTING_REJECTED",
                title="WooCommerce Listing Rejected",
                detail="WooCommerce listing requires a non-empty SKU and title.",
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
        html_desc = "".join(desc_parts) or f"<p>{html.escape(clean_title)}</p>"
        image_list = [
            {"src": url.strip()}
            for url in (images or ())
            if url and url.strip().startswith("https://")
        ]

        variant_list = list(variants or ())
        if not variant_list:
            payload: dict[str, Any] = {
                "name": clean_title,
                "type": "simple",
                "sku": clean_sku,
                "regular_price": str(norm_price),
                "description": html_desc,
                "manage_stock": True,
                "stock_quantity": max(0, int(quantity)),
                "status": "publish",
            }
            if image_list:
                payload["images"] = image_list[:10]
            created = await self._request("POST", "/products", json_body=payload)
            product_id = str((created or {}).get("id") or "").strip()
            if not product_id:
                raise AppError(
                    slug="woocommerce-listing-rejected",
                    status_code=502,
                    code="ERR_WOOCOMMERCE_LISTING_REJECTED",
                    title="WooCommerce Listing Rejected",
                    detail=f"WooCommerce returned no product id for '{clean_sku}'.",
                )
            return WooListingReceipt(
                external_product_id=product_id,
                external_sku=clean_sku,
                status=str(created.get("status") or "publish"),
                variant_receipts=[
                    {
                        "external_sku": clean_sku,
                        "external_variant_id": product_id,
                        "external_inventory_item_id": None,
                    }
                ],
                raw_response=dict(created),
            )

        option_map: dict[str, list[str]] = {}
        for opt in options or ():
            name = str(opt.get("name") or "").strip()
            if name:
                option_map[name] = [
                    str(v).strip() for v in opt.get("values") or () if str(v).strip()
                ]
        for var in variant_list:
            for k, val in (var.get("option_values") or {}).items():
                vals = option_map.setdefault(str(k), [])
                if str(val) not in vals:
                    vals.append(str(val))
        wc_attributes = [
            {
                "name": name,
                "visible": True,
                "variation": True,
                "options": vals,
            }
            for name, vals in option_map.items()
            if vals
        ]
        parent_payload: dict[str, Any] = {
            "name": clean_title,
            "type": "variable",
            "sku": f"{clean_sku}-SPU",
            "description": html_desc,
            "status": "publish",
            "attributes": wc_attributes,
        }
        if image_list:
            parent_payload["images"] = image_list[:10]
        created = await self._request("POST", "/products", json_body=parent_payload)
        product_id = str((created or {}).get("id") or "").strip()
        if not product_id:
            raise AppError(
                slug="woocommerce-listing-rejected",
                status_code=502,
                code="ERR_WOOCOMMERCE_LISTING_REJECTED",
                title="WooCommerce Listing Rejected",
                detail=f"WooCommerce returned no variable product id for '{clean_sku}'.",
            )
        create_variations = [
            {
                "sku": str(var.get("external_sku") or clean_sku),
                "regular_price": str(
                    Decimal(str(var.get("price_usd") or norm_price)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                ),
                "manage_stock": True,
                "stock_quantity": max(
                    0,
                    int(var["quantity"]) if var.get("quantity") is not None else quantity,
                ),
                "attributes": [
                    {"name": str(k), "option": str(val)}
                    for k, val in (var.get("option_values") or {}).items()
                ],
            }
            for var in variant_list
        ]
        batch_res = await self._request(
            "POST",
            f"/products/{product_id}/variations/batch",
            json_body={"create": create_variations},
        )
        created_vars = (batch_res or {}).get("create") or []
        variant_receipts = [
            {
                "external_sku": str(v.get("sku") or clean_sku),
                "external_variant_id": str(v.get("id") or "") or None,
                "external_inventory_item_id": None,
            }
            for v in created_vars
            if isinstance(v, Mapping)
        ]
        return WooListingReceipt(
            external_product_id=product_id,
            external_sku=str(variant_list[0].get("external_sku") or clean_sku),
            status=str(created.get("status") or "publish"),
            variant_receipts=variant_receipts,
            raw_response={"product": created, "variations": batch_res},
        )

    async def push_inventory_level(
        self,
        *,
        external_sku: str,
        quantity: int,
        external_product_id: str | None = None,
        external_variant_id: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        """Update WooCommerce product or variation ``stock_quantity`` via REST v3."""
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="woocommerce-inventory-sync-failed",
                status_code=422,
                code="ERR_WOOCOMMERCE_INVENTORY_SYNC",
                title="WooCommerce Inventory Sync Failed",
                detail="WooCommerce inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))
        pid = (external_product_id or "").strip() or None
        vid = (external_variant_id or "").strip() or None
        if not pid:
            found = await self._request("GET", "/products", params={"sku": clean_sku})
            if isinstance(found, list) and found and isinstance(found[0], Mapping):
                row = found[0]
                parent_id = str(row.get("parent_id") or "").strip()
                row_id = str(row.get("id") or "").strip()
                if parent_id and parent_id != "0":
                    pid = parent_id
                    vid = vid or row_id
                else:
                    pid = row_id
        if not pid:
            raise AppError(
                slug="woocommerce-inventory-sync-failed",
                status_code=502,
                code="ERR_WOOCOMMERCE_INVENTORY_SYNC",
                title="WooCommerce Inventory Sync Failed",
                detail=f"Could not resolve WooCommerce product id for SKU '{clean_sku}'.",
            )
        path = f"/products/{pid}/variations/{vid}" if vid and vid != pid else f"/products/{pid}"
        res = await self._request(
            "PUT",
            path,
            json_body={"manage_stock": True, "stock_quantity": norm_qty},
        )
        return {
            "channel": "woocommerce",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "product_id": pid,
            "variant_id": vid,
            "raw": res if isinstance(res, dict) else {},
        }

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_woocommerce_webhook(raw_body, headers, secret or self.webhook_secret)
