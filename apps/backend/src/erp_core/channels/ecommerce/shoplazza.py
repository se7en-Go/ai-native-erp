"""Shoplazza connector: dual HMAC verification (OAuth hex, webhook base64)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

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

SHOPLAZZA_API_VERSION = "2024-07"


def verify_shoplazza_oauth_hmac(query_params: Mapping[str, str], client_secret: str) -> bool:
    received = query_params.get("hmac", "")
    if not received or not client_secret:
        return False
    canonical = "&".join(
        f"{key}={value}"
        for key, value in sorted(query_params.items())
        if key not in {"hmac", "signature"}
    )
    expected = hmac.new(client_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


def verify_shoplazza_webhook(
    raw_body: bytes, headers: Mapping[str, str], client_secret: str
) -> bool:
    signature = (
        headers.get("X-Shoplazza-Hmac-Sha256")
        or headers.get("x-shoplazza-hmac-sha256")
        or headers.get("X-Shoplazza-Hmac-Sha256".lower())
        or ""
    )
    if not signature or not client_secret:
        return False
    expected = base64.b64encode(
        hmac.new(client_secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature.strip())


class ShoplazzaAdapter(CommercePlatformAdapter):
    """Shoplazza OpenAPI adapter with client-credentials bearer tokens."""

    def __init__(
        self,
        store_domain: str,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
        webhook_secret: str = "",
    ) -> None:
        self.store_domain = store_domain.strip().lower()
        self.access_token = access_token
        self._http_client = http_client
        self.webhook_secret = webhook_secret

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        url = f"https://{self.store_domain}/openapi/{SHOPLAZZA_API_VERSION}{path}"
        headers = {"Access-Token": self.access_token, "Content-Type": "application/json"}
        if self._http_client is not None:
            response = await self._http_client.request(
                method, url, params=params, json=json_body, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AppError(
                slug="shoplazza-invalid-response",
                status_code=502,
                code="ERR_SHOPLAZZA_INVALID_RESPONSE",
                title="Shoplazza Invalid Response",
                detail="Expected a JSON object.",
            )
        return payload

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value if value not in (None, "") else "0"))

    def _normalize_line_item(self, item: Mapping[str, Any]) -> NormalizedLineItem | None:
        """Build one line item from an explicit ``total`` or ``price * quantity``.

        Shoplazza's ``price`` is per unit; falling back to it as the line total
        under-reports multi-unit lines (and therefore revenue and CVP).
        """
        sku = str(item.get("sku") or "").strip()
        if not sku:
            return None
        quantity = int(item.get("quantity") or 1)
        explicit_total = item.get("total")
        line_total = (
            self._decimal(explicit_total)
            if explicit_total not in (None, "")
            else (self._decimal(item.get("price")) * Decimal(quantity)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        return NormalizedLineItem(
            external_sku=sku,
            title=str(item.get("title") or sku),
            quantity=quantity,
            line_total=line_total,
        )

    def _normalize_order(self, order: dict[str, Any]) -> NormalizedOrder:
        customer = order.get("customer") or {}
        company = (order.get("company") or {}).get("name") if order.get("company") else None
        metadata: dict[str, Any] = {"shoplazza_order_number": order.get("number")}
        if order.get("note"):
            metadata["po_number"] = order["note"]
        financial = str(order.get("financial_status") or "").lower()
        payment_status = (
            PaymentStatus.PAID
            if financial in {"paid", "partially_refunded"}
            else PaymentStatus.REFUNDED
            if financial == "refunded"
            else PaymentStatus.UNPAID
        )
        name = f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}".strip()
        return NormalizedOrder(
            external_order_id=str(order["id"]),
            external_order_number=str(order.get("number") or order["id"]),
            order_mode=OrderMode.B2B_WHOLESALE if company else OrderMode.B2C_RETAIL,
            currency=str(order.get("currency") or "USD"),
            subtotal_amount=self._decimal(order.get("subtotal_price")),
            shipping_charged=self._decimal(order.get("total_shipping_price")),
            tax_collected=self._decimal(order.get("total_tax")),
            total_amount=self._decimal(order.get("total_price")),
            fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=customer.get("email"),
                phone_e164=customer.get("phone"),
                external_customer_id=str(customer.get("id") or "") or None,
                external_identity_type=IdentityType.EMAIL,
                display_name=name or customer.get("email") or "Shoplazza Buyer",
                country_code=customer.get("country_code"),
            ),
            b2b_company=(
                NormalizedB2BCompany(legal_name=str(company), country_code="US")
                if company
                else None
            ),
            line_items=[
                line
                for item in order.get("line_items") or []
                if (line := self._normalize_line_item(item)) is not None
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
            "/orders",
            params={
                "updated_at_min": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 50,
                "page": int(cursor or 1),
            },
        )
        rows = payload.get("orders") or []
        return [self._normalize_order(row) for row in rows], (
            str(int(cursor or 1) + 1) if len(rows) == 50 else None
        )

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        payload = await self._request(
            "GET",
            "/products",
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
        return await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments",
            json_body={
                "tracking_number": tracking_number,
                "tracking_company": carrier_company,
                "tracking_url": tracking_url,
                "notify_customer": notify_customer,
            },
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shoplazza_webhook(raw_body, headers, secret or self.webhook_secret)
