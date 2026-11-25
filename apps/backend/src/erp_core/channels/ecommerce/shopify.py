"""Shopify Admin GraphQL connector: client-credentials refresh, B2B, GDPR webhooks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
    attach_financial_metadata,
    register_channel_routing,
    resolve_routing_tenant,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.gdpr_service import GdprService

SHOPIFY_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$")
SHOPIFY_API_VERSION = "2026-01"
SHOPIFY_API_PATH = f"/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

ORDERS_GRAPHQL_QUERY = """
query SyncOrders($first: Int!, $query: String, $after: String) {
  orders(first: $first, query: $query, after: $after, sortKey: UPDATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      updatedAt
      cancelledAt
      displayFinancialStatus
      displayFulfillmentStatus
      currencyCode
      poNumber
      currentSubtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } }
      totalTaxSet { shopMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
      customer { id email phone firstName lastName }
      shippingAddress { countryCodeV2 }
      purchasingEntity {
        __typename
        ... on PurchasingCompany {
          company { id name externalId }
          location { id name externalId shippingAddress { countryCode } }
        }
      }
      lineItems(first: 100) {
        nodes { sku title quantity originalTotalSet { shopMoney { amount currencyCode } } }
      }
    }
  }
}
"""

PRODUCTS_GRAPHQL_QUERY = """
query SyncProducts($first: Int!, $query: String, $after: String) {
  products(first: $first, query: $query, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      variants(first: 100) { nodes { id sku price inventoryItem { id } } }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation CreateFulfillment($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment {
      id
      status
      trackingInfo { company number url }
    }
    userErrors { field message }
  }
}
"""

ORDER_FULFILLMENT_ORDERS_QUERY = """
query OrderFulfillmentOrders($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 20) {
      nodes {
        id
        status
        supportedActions { action }
      }
    }
  }
}
"""

PRODUCT_SET_MUTATION = """
mutation PublishProductSet($synchronous: Boolean!, $productSet: ProductSetInput!) {
  productSet(synchronous: $synchronous, input: $productSet) {
    product {
      id
      title
      handle
      status
      variants(first: 50) {
        nodes {
          id
          sku
          price
          inventoryItem {
            id
          }
        }
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

STAGED_UPLOADS_CREATE_MUTATION = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters {
        name
        value
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

LOCATIONS_QUERY = """
query PrimaryLocations {
  locations(first: 1) {
    nodes {
      id
    }
  }
}
"""

VARIANT_BY_SKU_QUERY = """
query VariantBySku($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes {
      id
      sku
      inventoryItem {
        id
        inventoryLevels(first: 1) {
          nodes {
            location {
              id
            }
          }
        }
      }
    }
  }
}
"""

INVENTORY_ITEM_LOCATION_QUERY = """
query InventoryItemLocations($id: ID!) {
  inventoryItem(id: $id) {
    id
    inventoryLevels(first: 1) {
      nodes {
        location {
          id
        }
      }
    }
  }
}
"""

INVENTORY_SET_QUANTITIES_MUTATION = """
mutation InventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup {
      reason
      changes {
        name
        delta
        quantityAfterChange
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""


class ShopifyListingReceipt(BaseModel):
    """Receipt returned after Shopify Admin GraphQL ``productSet`` confirms a listing."""

    external_product_id: str
    external_sku: str
    status: str = "ACTIVE"
    handle: str = ""
    variant_receipts: list[dict[str, Any]] = Field(default_factory=list)
    raw_response: dict[str, Any] = Field(default_factory=dict)


def _format_shopify_description_html(
    *,
    title: str,
    description: str,
    bullet_points: Sequence[str],
    moq_tiers: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render escaped semantic HTML for Shopify's ``ProductSetInput.descriptionHtml``."""
    blocks: list[str] = []
    bullets_block: list[str] = []
    description_lines = [line.strip() for line in description.splitlines() if line.strip()]
    for line in description_lines:
        if line.startswith("- ") or line.startswith("* "):
            bullets_block.append(f"<li>{html.escape(line[2:].strip())}</li>")
        else:
            if bullets_block:
                blocks.append(f"<ul>{''.join(bullets_block)}</ul>")
                bullets_block.clear()
            blocks.append(f"<p>{html.escape(line)}</p>")
    if bullets_block:
        blocks.append(f"<ul>{''.join(bullets_block)}</ul>")

    if not blocks:
        blocks.append(f"<p>{html.escape(title)}</p>")

    normalized_desc = description.casefold()
    extra_bullets = [
        item.strip()
        for item in bullet_points
        if item.strip() and item.strip().casefold() not in normalized_desc
    ]
    if extra_bullets:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in extra_bullets)
        blocks.append(f"<ul>{items}</ul>")

    tier_items: list[str] = []
    for tier in moq_tiers or ():
        qty = tier.get("min_qty") or tier.get("min_quantity")
        unit_price = tier.get("unit_price_usd") or tier.get("unit_price")
        if qty is not None and unit_price is not None:
            tier_items.append(
                f"<li>MOQ {html.escape(str(qty))}+: ${html.escape(str(unit_price))} USD</li>"
            )
    if tier_items and "moq" not in normalized_desc:
        blocks.append(f"<ul>{''.join(tier_items)}</ul>")

    return "".join(blocks)


def validate_shop_domain(shop: str) -> str:
    cleaned = shop.strip().lower()
    if not SHOPIFY_DOMAIN_RE.match(cleaned):
        raise AppError(
            slug="invalid-shopify-domain",
            status_code=422,
            code="ERR_INVALID_SHOPIFY_DOMAIN",
            title="Invalid Shopify Domain",
            detail="Shop domain must match '<store>.myshopify.com'.",
        )
    return cleaned


def build_shopify_oauth_install_url(
    shop: str,
    redirect_uri: str,
    state: str,
    scopes: str = (
        "read_orders,write_orders,read_products,write_products,"
        "read_inventory,write_inventory,read_locations,write_fulfillments,"
        "read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders,"
        "read_customers,read_companies"
    ),
    client_id: str | None = None,
) -> str:
    domain = validate_shop_domain(shop)
    app_id = client_id if client_id is not None else os.getenv("SHOPIFY_CLIENT_ID", "")
    if not app_id:
        raise AppError(
            slug="shopify-client-id-missing",
            status_code=503,
            code="ERR_SHOPIFY_CLIENT_ID_MISSING",
            title="Shopify App Not Configured",
            detail="SHOPIFY_CLIENT_ID is not configured.",
        )
    query = urlencode(
        {"client_id": app_id, "scope": scopes, "redirect_uri": redirect_uri, "state": state}
    )
    return f"https://{domain}/admin/oauth/authorize?{query}"


def verify_shopify_oauth_hmac(query_params: Mapping[str, str], client_secret: str) -> bool:
    received = query_params.get("hmac", "")
    if not received or not client_secret:
        return False
    message = "&".join(
        f"{key}={value}" for key, value in sorted(query_params.items()) if key != "hmac"
    ).encode()
    expected = hmac.new(client_secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.lower())


def verify_shopify_webhook_hmac(raw_body: bytes, hmac_header: str, secret: str) -> bool:
    if not hmac_header or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), hmac_header.strip())


class ShopifyGdprPort(Protocol):
    async def handle_customer_data_request(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None: ...

    async def handle_customer_redact(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None: ...

    async def handle_shop_redact(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None: ...


class TenancyGdprBridge:
    """Delegates the three mandatory Shopify GDPR topics to the tenancy GDPR service."""

    def __init__(
        self,
        crypto: EnvelopeCryptoService | None = None,
        storage: ObjectStorageService | None = None,
        gdpr_service: GdprService | None = None,
    ) -> None:
        settings = get_settings()
        self.crypto = crypto or EnvelopeCryptoService(settings)
        self.storage = storage or ObjectStorageService(settings)
        self.gdpr = gdpr_service or GdprService()

    @staticmethod
    async def _resolve_customer_id(
        session: AsyncSession, tenant_id: UUID, customer_payload: dict[str, Any]
    ) -> UUID | None:
        external_id = str(customer_payload.get("id") or "").strip()
        email = (customer_payload.get("email") or "").strip().lower()
        if external_id:
            customer_id = await session.scalar(
                text(
                    "SELECT customer_id FROM customer_identities "
                    "WHERE tenant_id = :tid AND identity_type = 'shopify_customer_id' "
                    "AND identity_value = :val LIMIT 1"
                ),
                {"tid": tenant_id, "val": external_id},
            )
            if customer_id is not None:
                return UUID(str(customer_id))
        if email:
            found = await session.scalar(
                text(
                    "SELECT id FROM customers WHERE tenant_id = :tid "
                    "AND lower(primary_email) = :email LIMIT 1"
                ),
                {"tid": tenant_id, "email": email},
            )
            if found is not None:
                return UUID(str(found))
        return None

    async def handle_customer_data_request(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None:
        customer_id = await self._resolve_customer_id(
            session, tenant_id, payload.get("customer") or {}
        )
        if customer_id is None:
            return
        await self.gdpr.export_customer_data(
            session,
            self.storage,
            tenant_id=tenant_id,
            customer_id=customer_id,
            actor_id="shopify_gdpr_webhook",
        )

    async def handle_customer_redact(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None:
        customer_id = await self._resolve_customer_id(
            session, tenant_id, payload.get("customer") or {}
        )
        if customer_id is None:
            return
        await self.gdpr.anonymize_customer_pii(
            session,
            tenant_id=tenant_id,
            customer_id=customer_id,
            actor_id="shopify_gdpr_webhook",
        )

    async def handle_shop_redact(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None:
        await self.gdpr.redact_tenant_crypto_shred(
            session,
            self.storage,
            self.crypto,
            tenant_id=tenant_id,
            actor_id="shopify_gdpr_webhook",
        )


class ShopifyGraphqlAdapter(CommercePlatformAdapter):
    """Shopify Admin GraphQL connector with 24h client-credentials auto-refresh."""

    verify_webhook_signature = staticmethod(verify_shopify_webhook_hmac)

    def __init__(
        self,
        shop_domain: str,
        access_token: str,
        webhook_secret: str = "",
        auth_mode: str = "oauth",
        client_id: str | None = None,
        client_secret: str | None = None,
        expires_at: datetime | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        tenant_id: UUID | None = None,
        channel_account_id: UUID | None = None,
        vault: CredentialVaultHelper | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.shop_domain = validate_shop_domain(shop_domain)
        self.access_token = access_token
        self.webhook_secret = webhook_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "")
        self.auth_mode = auth_mode
        self.client_id = client_id
        self.client_secret = client_secret
        self.expires_at = expires_at
        self._http_client = http_client
        self._transport = transport
        self._tenant_id = tenant_id
        self._channel_account_id = channel_account_id
        self._vault = vault
        self._session = session
        self._default_location_id: str | None = None

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ShopifyGraphqlAdapter:
        vault = CredentialVaultHelper(crypto)
        try:
            data, expires_at = await vault.load_secret_json(
                session, tenant_id, channel_account_id, "shopify_custom_app"
            )
        except AppError:
            data, expires_at = await vault.load_secret_json(
                session, tenant_id, channel_account_id, "oauth_token"
            )
        return cls(
            shop_domain=data["shop_domain"],
            access_token=data.get("access_token", ""),
            webhook_secret=str(data.get("client_secret") or os.getenv("SHOPIFY_CLIENT_SECRET", "")),
            auth_mode=data.get("auth_mode", "oauth"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            expires_at=expires_at,
            http_client=http_client,
            transport=transport,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            vault=vault,
            session=session,
        )

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(url, json=payload, headers=headers)
        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            return await client.post(url, json=payload, headers=headers)

    async def _ensure_valid_access_token(self) -> None:
        if self.auth_mode != "client_credentials":
            return
        now = datetime.now(UTC)
        if self.access_token and self.expires_at and self.expires_at > now + timedelta(minutes=5):
            return
        if not self.client_id or not self.client_secret:
            raise AppError(
                slug="shopify-credentials-missing",
                status_code=401,
                code="ERR_SHOPIFY_CREDENTIALS_MISSING",
                title="Shopify Credentials Missing",
                detail="client_id and client_secret are required for token refresh.",
            )
        response = await self._post(
            f"https://{self.shop_domain}/admin/oauth/access_token",
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            {"Content-Type": "application/json"},
        )
        response.raise_for_status()
        token = response.json()
        if "access_token" not in token:
            raise AppError(
                slug="shopify-token-refresh-failed",
                status_code=502,
                code="ERR_SHOPIFY_TOKEN_REFRESH",
                title="Shopify Token Refresh Failed",
                detail="Shopify did not return an access token.",
            )
        self.access_token = str(token["access_token"])
        self.expires_at = now + timedelta(seconds=int(token.get("expires_in", 86399)))
        if self._vault and self._session and self._tenant_id and self._channel_account_id:
            await self._vault.save_secret_json(
                self._session,
                tenant_id=self._tenant_id,
                channel_account_id=self._channel_account_id,
                credential_type="shopify_custom_app",
                payload={
                    "shop_domain": self.shop_domain,
                    "auth_mode": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "access_token": self.access_token,
                },
                expires_at=self.expires_at,
            )

    async def _graphql(
        self, query: str, variables: dict[str, Any], min_cost_reserve: int = 80
    ) -> dict[str, Any]:
        await self._ensure_valid_access_token()
        url = f"https://{self.shop_domain}{SHOPIFY_API_PATH}"
        for attempt in range(4):
            response = await self._post(
                url,
                {"query": query, "variables": variables},
                {"X-Shopify-Access-Token": self.access_token, "Content-Type": "application/json"},
            )
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", str(2**attempt)))
                await asyncio.sleep(min(retry_after, 10.0))
                continue
            response.raise_for_status()
            body = response.json()
            errors = body.get("errors") or []
            cost = (body.get("extensions") or {}).get("cost") or {}
            throttle = cost.get("throttleStatus") or {}
            available = float(throttle.get("currentlyAvailable", 1000))
            restore_rate = max(float(throttle.get("restoreRate", 100)), 1.0)
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                await asyncio.sleep(
                    min(max((min_cost_reserve - available) / restore_rate, 0.5), 10.0)
                )
                continue
            if errors:
                raise AppError(
                    slug="shopify-graphql-error",
                    status_code=502,
                    code="ERR_SHOPIFY_GRAPHQL",
                    title="Shopify GraphQL Error",
                    detail=json.dumps(errors),
                )
            payload: dict[str, Any] = body["data"]
            return payload
        raise AppError(
            slug="shopify-rate-limit-exhausted",
            status_code=429,
            code="ERR_SHOPIFY_THROTTLED",
            title="Shopify Rate Limit Exhausted",
            detail="Shopify GraphQL cost bucket remained exhausted after retries.",
        )

    @staticmethod
    def _normalize_order_node(node: dict[str, Any]) -> NormalizedOrder:
        customer_node = node.get("customer") or {}
        shipping_address = node.get("shippingAddress") or {}
        purchasing = node.get("purchasingEntity") or {}
        b2b_company: NormalizedB2BCompany | None = None
        order_mode = OrderMode.B2C_RETAIL
        channel_metadata: dict[str, Any] = {
            "shopify_order_name": node.get("name"),
            "financial_status": node.get("displayFinancialStatus"),
            "fulfillment_status": node.get("displayFulfillmentStatus"),
        }
        cancelled_at = node.get("cancelledAt")
        if cancelled_at:
            channel_metadata["cancelled_at"] = str(cancelled_at)
        if node.get("poNumber"):
            channel_metadata["po_number"] = node["poNumber"]
        if purchasing.get("__typename") == "PurchasingCompany" and purchasing.get("company"):
            order_mode = OrderMode.B2B_WHOLESALE
            company = purchasing["company"]
            location = purchasing.get("location") or {}
            country = (
                (location.get("shippingAddress") or {}).get("countryCode")
                or (location.get("shippingAddress") or {}).get("countryCodeV2")
                or shipping_address.get("countryCodeV2")
                or "US"
            )
            b2b_company = NormalizedB2BCompany(
                legal_name=company["name"],
                country_code=country,
                external_company_id=company.get("id"),
                location_id=location.get("id"),
            )
            channel_metadata["shopify_company_id"] = company.get("id")
            channel_metadata["shopify_company_location_id"] = location.get("id")

        financial = str(node.get("displayFinancialStatus") or "").upper()
        payment_status = {
            "PAID": PaymentStatus.PAID,
            "PARTIALLY_PAID": PaymentStatus.PARTIALLY_PAID,
            "PARTIALLY_REFUNDED": PaymentStatus.PAID,
            "REFUNDED": PaymentStatus.REFUNDED,
        }.get(financial, PaymentStatus.UNPAID)
        fulfillment = str(node.get("displayFulfillmentStatus") or "").upper()
        if cancelled_at:
            fulfillment_status = FulfillmentStatus.CANCELLED
        elif financial == "REFUNDED":
            fulfillment_status = FulfillmentStatus.REFUNDED
        else:
            fulfillment_status = {
                "FULFILLED": FulfillmentStatus.SHIPPED,
                "PARTIALLY_FULFILLED": FulfillmentStatus.PARTIALLY_SHIPPED,
            }.get(fulfillment, FulfillmentStatus.CONFIRMED_RESERVED)

        def money(field: str) -> Decimal:
            shop_money = (node.get(field) or {}).get("shopMoney") or {}
            return Decimal(str(shop_money.get("amount", "0.00")))

        refunded_val = (
            money("totalRefundedSet")
            if node.get("totalRefundedSet")
            else Decimal(str(node.get("refunded_amount") or "0.00"))
        )
        commission_val = Decimal(
            str(node.get("platform_commission_base") or node.get("platform_commission") or "0.00")
        )
        attach_financial_metadata(
            channel_metadata,
            refunded_amount=refunded_val,
            platform_commission_base=commission_val,
        )

        first = customer_node.get("firstName") or ""
        last = customer_node.get("lastName") or ""
        items = [
            NormalizedLineItem(
                external_sku=sku,
                title=line_item.get("title") or sku,
                quantity=int(line_item["quantity"]),
                line_total=Decimal(
                    str(
                        ((line_item.get("originalTotalSet") or {}).get("shopMoney") or {}).get(
                            "amount", "0"
                        )
                    )
                ),
            )
            for line_item in (node.get("lineItems") or {}).get("nodes") or []
            if (sku := (line_item.get("sku") or "").strip())
        ]
        return NormalizedOrder(
            external_order_id=str(node["id"]),
            external_order_number=node.get("name"),
            order_mode=order_mode,
            currency=str(node.get("currencyCode") or "USD"),
            subtotal_amount=money("currentSubtotalPriceSet"),
            shipping_charged=money("totalShippingPriceSet"),
            tax_collected=money("totalTaxSet"),
            total_amount=money("currentTotalPriceSet"),
            platform_commission_base=commission_val.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer=NormalizedCustomer(
                email=customer_node.get("email"),
                phone_e164=customer_node.get("phone"),
                external_customer_id=customer_node.get("id"),
                external_identity_type=IdentityType.SHOPIFY_CUSTOMER_ID,
                display_name=(
                    f"{first} {last}".strip() or customer_node.get("email") or "Shopify Buyer"
                ),
                country_code=shipping_address.get("countryCodeV2"),
            ),
            b2b_company=b2b_company,
            line_items=items,
            channel_metadata=channel_metadata,
            updated_at=datetime.fromisoformat(str(node["updatedAt"]).replace("Z", "+00:00")),
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._graphql(
            ORDERS_GRAPHQL_QUERY,
            {"first": 50, "query": f"updated_at:>='{iso}'", "after": cursor},
        )
        connection = data["orders"]
        orders = [
            self._normalize_order_node(node)
            for node in connection["nodes"]
            if (node.get("lineItems") or {}).get("nodes")
        ]
        page_info = connection["pageInfo"]
        return orders, (page_info["endCursor"] if page_info.get("hasNextPage") else None)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._graphql(
            PRODUCTS_GRAPHQL_QUERY,
            {"first": 50, "query": f"updated_at:>='{iso}'", "after": cursor},
        )
        connection = data["products"]
        products = [
            NormalizedProduct(
                external_product_id=product["id"],
                title=product["title"],
                variants=(product.get("variants") or {}).get("nodes") or [],
            )
            for product in connection["nodes"]
        ]
        page_info = connection["pageInfo"]
        return products, (page_info["endCursor"] if page_info.get("hasNextPage") else None)

    async def _resolve_open_fulfillment_order_id(self, external_order_id: str) -> str:
        data = await self._graphql(
            ORDER_FULFILLMENT_ORDERS_QUERY, {"id": external_order_id}
        )
        nodes: list[dict[str, Any]] = (
            ((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes") or []
        )
        open_nodes = [
            node
            for node in nodes
            if str(node.get("status") or "").upper() in {"OPEN", "IN_PROGRESS", "SCHEDULED"}
            and node.get("id")
        ]
        for node in open_nodes:
            actions = {
                str(a.get("action") or "").upper()
                for a in (node.get("supportedActions") or [])
                if isinstance(a, Mapping)
            }
            if "CREATE_FULFILLMENT" in actions:
                return str(node["id"])
        if open_nodes:
            return str(open_nodes[0]["id"])
        raise AppError(
            slug="shopify-fulfillment-order-not-found",
            status_code=422,
            code="ERR_SHOPIFY_FULFILLMENT_ORDER_NOT_FOUND",
            title="Shopify Fulfillment Order Not Found",
            detail=f"No open fulfillment order found for '{external_order_id}'.",
        )

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        resolved_fo_id = (fulfillment_order_id or "").strip()
        if not resolved_fo_id or (
            external_order_id.startswith("gid://shopify/Order/")
            and not resolved_fo_id.startswith("gid://shopify/FulfillmentOrder/")
        ):
            if external_order_id.startswith("gid://shopify/Order/"):
                resolved_fo_id = await self._resolve_open_fulfillment_order_id(external_order_id)
            elif not resolved_fo_id:
                resolved_fo_id = external_order_id
        tracking_info: dict[str, Any] = {"number": tracking_number, "company": carrier_company}
        if tracking_url:
            tracking_info["url"] = tracking_url
        data = await self._graphql(
            FULFILLMENT_CREATE_MUTATION,
            {
                "fulfillment": {
                    "notifyCustomer": notify_customer,
                    "trackingInfo": tracking_info,
                    "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": resolved_fo_id}],
                }
            },
        )
        result: dict[str, Any] = data["fulfillmentCreate"]
        if result.get("userErrors"):
            raise AppError(
                slug="shopify-fulfillment-error",
                status_code=422,
                code="ERR_SHOPIFY_FULFILLMENT",
                title="Shopify Fulfillment Error",
                detail=json.dumps(result["userErrors"]),
            )
        return result

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        signature = (
            headers.get("X-Shopify-Hmac-SHA256") or headers.get("x-shopify-hmac-sha256") or ""
        )
        return verify_shopify_webhook_hmac(raw_body, signature, secret)

    async def _upload_staged_images(
        self,
        blobs: Sequence[Mapping[str, Any]],
        fallback_alt: str,
    ) -> list[dict[str, str]]:
        if not blobs:
            return []
        staged_input = [
            {
                "filename": str(blob.get("filename") or f"image-{idx + 1}.jpg"),
                "mimeType": str(blob.get("mime_type") or "image/jpeg"),
                "httpMethod": "POST",
                "resource": "PRODUCT_IMAGE",
                "fileSize": str(len(bytes(blob["data"]))),
            }
            for idx, blob in enumerate(blobs)
            if blob.get("data")
        ]
        if not staged_input:
            return []
        data = await self._graphql(
            STAGED_UPLOADS_CREATE_MUTATION, {"input": staged_input}
        )
        staged = data.get("stagedUploadsCreate") or {}
        user_errors = staged.get("userErrors") or []
        targets = staged.get("stagedTargets") or []
        if user_errors or len(targets) != len(staged_input):
            summary = "; ".join(
                str(err.get("message") or err) for err in user_errors
            ) or "Shopify stagedUploadsCreate returned incomplete targets."
            raise AppError(
                slug="shopify-media-upload-failed",
                status_code=502,
                code="ERR_SHOPIFY_MEDIA_UPLOAD",
                title="Shopify Media Upload Failed",
                detail=summary,
            )

        uploaded: list[dict[str, str]] = []
        client = self._http_client
        owned = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=30.0, transport=self._transport)
        try:
            for blob, target in zip(blobs, targets, strict=True):
                upload_url = str(target.get("url") or "")
                resource_url = str(target.get("resourceUrl") or "")
                if not upload_url or not resource_url:
                    continue
                params = {
                    str(p["name"]): str(p["value"])
                    for p in (target.get("parameters") or [])
                    if p.get("name")
                }
                fname = str(blob.get("filename") or "image.jpg")
                mime = str(blob.get("mime_type") or "image/jpeg")
                resp = await client.post(
                    upload_url,
                    data=params,
                    files={"file": (fname, bytes(blob["data"]), mime)},
                )
                resp.raise_for_status()
                uploaded.append(
                    {
                        "originalSource": resource_url,
                        "contentType": "IMAGE",
                        "alt": str(blob.get("alt_text") or fallback_alt)[:255],
                    }
                )
        finally:
            if owned:
                await client.aclose()
        return uploaded

    @staticmethod
    def _build_product_options_and_variants(
        *,
        clean_sku: str,
        normalized_price: Decimal,
        options: Sequence[Mapping[str, Any]] | None,
        variants: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        variant_rows = list(variants or ())
        if not variant_rows:
            return (
                [{"name": "Title", "values": [{"name": "Default Title"}]}],
                [
                    {
                        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                        "sku": clean_sku,
                        "price": str(normalized_price),
                        "inventoryItem": {"tracked": True},
                    }
                ],
            )

        option_values_by_name: dict[str, list[str]] = {}
        for opt in options or ():
            name = str(opt.get("name") or "").strip()
            if not name:
                continue
            vals = option_values_by_name.setdefault(name, [])
            for raw_val in opt.get("values") or ():
                val_str = str(raw_val).strip()
                if val_str and val_str not in vals:
                    vals.append(val_str)

        built_variants: list[dict[str, Any]] = []
        for idx, var in enumerate(variant_rows, start=1):
            v_sku = str(var.get("external_sku") or clean_sku).strip()
            v_price = Decimal(str(var.get("price_usd") or normalized_price)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if v_price <= Decimal("0") or not v_sku:
                raise AppError(
                    slug="shopify-listing-rejected",
                    status_code=422,
                    code="ERR_SHOPIFY_LISTING_REJECTED",
                    title="Shopify Listing Rejected",
                    detail="Every Shopify variant requires a non-empty SKU and positive price.",
                )
            opt_map = var.get("option_values") or {}
            opt_pairs: list[dict[str, str]] = []
            if isinstance(opt_map, Mapping) and opt_map:
                for k, val in opt_map.items():
                    k_str = str(k).strip() or "Option"
                    v_str = str(val).strip() or f"Option {idx}"
                    vals = option_values_by_name.setdefault(k_str, [])
                    if v_str not in vals:
                        vals.append(v_str)
                    opt_pairs.append({"optionName": k_str, "name": v_str})
            else:
                vals = option_values_by_name.setdefault("Variant", [])
                v_str = str(var.get("local_sku") or v_sku or f"Option {idx}")
                if v_str not in vals:
                    vals.append(v_str)
                opt_pairs.append({"optionName": "Variant", "name": v_str})
            built_variants.append(
                {
                    "optionValues": opt_pairs,
                    "sku": v_sku,
                    "price": str(v_price),
                    "inventoryItem": {"tracked": True},
                }
            )

        if not option_values_by_name:
            option_values_by_name["Title"] = ["Default Title"]
        product_options = [
            {"name": name, "values": [{"name": val} for val in vals]}
            for name, vals in option_values_by_name.items()
        ]
        return product_options, built_variants

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
    ) -> ShopifyListingReceipt:
        """Create or update an ``ACTIVE`` multi-variant product via Shopify ``productSet``."""
        del hs_code
        clean_sku = sku.strip()
        clean_title = title.strip()
        if not clean_sku or not clean_title:
            raise AppError(
                slug="shopify-listing-rejected",
                status_code=422,
                code="ERR_SHOPIFY_LISTING_REJECTED",
                title="Shopify Listing Rejected",
                detail="Shopify productSet requires both a non-empty SKU and title.",
            )
        normalized_price = Decimal(str(price_usd)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if normalized_price <= Decimal("0") or quantity < 0:
            raise AppError(
                slug="shopify-listing-rejected",
                status_code=422,
                code="ERR_SHOPIFY_LISTING_REJECTED",
                title="Shopify Listing Rejected",
                detail=(
                    "Shopify productSet requires a positive USD price and non-negative quantity."
                ),
            )

        product_options, variant_inputs = self._build_product_options_and_variants(
            clean_sku=clean_sku,
            normalized_price=normalized_price,
            options=options,
            variants=variants,
        )
        product_input: dict[str, Any] = {
            "title": clean_title,
            "descriptionHtml": _format_shopify_description_html(
                title=clean_title,
                description=description,
                bullet_points=bullet_points,
                moq_tiers=moq_tiers,
            ),
            "status": "ACTIVE",
            "productOptions": product_options,
            "variants": variant_inputs,
        }
        clean_category = category.strip()
        if clean_category:
            product_input["productType"] = clean_category

        files: list[dict[str, str]] = []
        if image_blobs:
            files.extend(await self._upload_staged_images(image_blobs, clean_title))
        for url in images or ():
            clean_url = (url or "").strip()
            if clean_url.startswith(("https://", "http://")):
                files.append({"originalSource": clean_url, "contentType": "IMAGE"})
        if files:
            product_input["files"] = files[:20]

        data = await self._graphql(
            PRODUCT_SET_MUTATION,
            {"synchronous": True, "productSet": product_input},
        )
        product_set = data.get("productSet") or {}
        user_errors = product_set.get("userErrors") or []
        product = product_set.get("product") or {}
        product_id = str(product.get("id") or "").strip()
        if user_errors or not product_id:
            error_summary = "; ".join(
                str(err.get("message") or err).strip() for err in user_errors
            ) or "Shopify productSet did not return a confirmed product id."
            raise AppError(
                slug="shopify-listing-rejected",
                status_code=422,
                code="ERR_SHOPIFY_LISTING_REJECTED",
                title="Shopify Listing Rejected",
                detail=f"Shopify rejected listing '{clean_sku}': {error_summary}",
            )

        variant_nodes = ((product.get("variants") or {}).get("nodes")) or []
        variant_receipts = [
            {
                "external_sku": str(node.get("sku") or clean_sku),
                "external_variant_id": str(node.get("id") or "") or None,
                "external_inventory_item_id": (
                    str((node.get("inventoryItem") or {}).get("id") or "") or None
                ),
            }
            for node in variant_nodes
            if isinstance(node, Mapping)
        ]
        confirmed_sku = (
            str(variant_nodes[0].get("sku") or "").strip() if variant_nodes else ""
        ) or clean_sku
        raw_payload = dict(product_set)
        raw_payload["requested_quantity"] = quantity
        return ShopifyListingReceipt(
            external_product_id=product_id,
            external_sku=confirmed_sku,
            status=str(product.get("status") or "ACTIVE"),
            handle=str(product.get("handle") or ""),
            variant_receipts=variant_receipts,
            raw_response=raw_payload,
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
        """Set available inventory quantity for one SKU via ``inventorySetQuantities``."""
        del external_product_id, external_variant_id
        clean_sku = external_sku.strip()
        if not clean_sku:
            raise AppError(
                slug="shopify-inventory-sync-failed",
                status_code=422,
                code="ERR_SHOPIFY_INVENTORY_SYNC",
                title="Shopify Inventory Sync Failed",
                detail="Shopify inventory sync requires a non-empty external_sku.",
            )
        norm_qty = max(0, int(quantity))

        item_id = (external_inventory_item_id or "").strip() or None
        stocked_loc_id: str | None = None
        if not item_id:
            var_data = await self._graphql(
                VARIANT_BY_SKU_QUERY, {"query": f"sku:{clean_sku}"}
            )
            var_nodes = ((var_data.get("productVariants") or {}).get("nodes")) or []
            if var_nodes:
                inv_item = var_nodes[0].get("inventoryItem") or {}
                item_id = str(inv_item.get("id") or "").strip() or None
                lvl_nodes = ((inv_item.get("inventoryLevels") or {}).get("nodes")) or []
                if lvl_nodes:
                    stocked_loc_id = (
                        str(((lvl_nodes[0].get("location") or {}).get("id")) or "").strip()
                        or None
                    )
        if not item_id:
            raise AppError(
                slug="shopify-inventory-sync-failed",
                status_code=502,
                code="ERR_SHOPIFY_INVENTORY_SYNC",
                title="Shopify Inventory Sync Failed",
                detail=(
                    f"Could not resolve Shopify inventoryItemId for external SKU '{clean_sku}'."
                ),
            )

        loc_id = (location_id or "").strip() or stocked_loc_id
        if not loc_id and (external_inventory_item_id or "").strip():
            try:
                lvl_data = await self._graphql(INVENTORY_ITEM_LOCATION_QUERY, {"id": item_id})
                lvl_nodes = (
                    (
                        (lvl_data.get("inventoryItem") or {}).get("inventoryLevels")
                        or {}
                    ).get("nodes")
                ) or []
                if lvl_nodes:
                    loc_id = (
                        str(((lvl_nodes[0].get("location") or {}).get("id")) or "").strip()
                        or None
                    )
            except AppError:
                loc_id = None
        if not loc_id:
            loc_id = self._default_location_id
        if not loc_id:
            loc_data = await self._graphql(LOCATIONS_QUERY, {})
            loc_nodes = ((loc_data.get("locations") or {}).get("nodes")) or []
            if not loc_nodes or not loc_nodes[0].get("id"):
                raise AppError(
                    slug="shopify-inventory-sync-failed",
                    status_code=502,
                    code="ERR_SHOPIFY_INVENTORY_SYNC",
                    title="Shopify Inventory Sync Failed",
                    detail=f"No Shopify fulfillment location found for store '{self.shop_domain}'.",
                )
            loc_id = str(loc_nodes[0]["id"])
            self._default_location_id = loc_id

        data = await self._graphql(
            INVENTORY_SET_QUANTITIES_MUTATION,
            {
                "input": {
                    "reason": "correction",
                    "name": "available",
                    "ignoreCompareQuantity": True,
                    "quantities": [
                        {
                            "inventoryItemId": item_id,
                            "locationId": loc_id,
                            "quantity": norm_qty,
                        }
                    ],
                }
            },
        )
        result = data.get("inventorySetQuantities") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            summary = "; ".join(str(err.get("message") or err) for err in user_errors)
            raise AppError(
                slug="shopify-inventory-sync-failed",
                status_code=502,
                code="ERR_SHOPIFY_INVENTORY_SYNC",
                title="Shopify Inventory Sync Failed",
                detail=f"Shopify rejected inventory update for '{clean_sku}': {summary}",
            )
        return {
            "channel": "shopify",
            "external_sku": clean_sku,
            "quantity": norm_qty,
            "external_inventory_item_id": item_id,
            "location_id": loc_id,
            "raw": result,
        }


GDPR_TOPICS = frozenset({"customers/data_request", "customers/redact", "shop/redact"})


async def process_shopify_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    topic: str,
    shop_domain: str,
    payload: dict[str, Any],
    gdpr_port: ShopifyGdprPort | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Apply a verified Shopify delivery inside the owning tenant's RLS context."""
    domain = validate_shop_domain(shop_domain)
    async with session_factory() as lookup:
        route = await resolve_routing_tenant(lookup, "shopify", domain)
    if route is None:
        return False
    tenant_id, channel_account_id = route
    gdpr = gdpr_port or TenancyGdprBridge()

    async with tenant_session(session_factory, tenant_id) as session:
        if topic == "customers/data_request":
            await gdpr.handle_customer_data_request(session, tenant_id, payload)
        elif topic == "customers/redact":
            await gdpr.handle_customer_redact(session, tenant_id, payload)
        elif topic == "shop/redact":
            await gdpr.handle_shop_redact(session, tenant_id, payload)
            await register_channel_routing(
                session,
                channel_type="shopify",
                external_account_id=domain,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                is_active=False,
            )
        elif topic in {"orders/create", "orders/updated", "orders/cancelled", "refunds/create"}:
            warehouse_id = channel_account_id
            configured = await session.scalar(
                text(
                    "SELECT config->>'warehouse_id' FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :caid"
                ),
                {"tid": tenant_id, "caid": channel_account_id},
            )
            if configured:
                warehouse_id = UUID(str(configured))
            adapter = await ShopifyGraphqlAdapter.from_channel_account(
                session, tenant_id, channel_account_id, http_client=http_client
            )
            orders, _ = await adapter.fetch_orders_updated_since(
                datetime.now(UTC) - timedelta(hours=1)
            )
            if topic == "refunds/create":
                order_raw_id = payload.get("order_id")
                target = f"gid://shopify/Order/{order_raw_id}" if order_raw_id is not None else ""
            else:
                target = (
                    payload.get("admin_graphql_api_id") or f"gid://shopify/Order/{payload.get('id')}"
                )
            sync = EcommerceOrderSyncService()
            for order in orders:
                if target and order.external_order_id == target:
                    await sync.ingest_normalized_order(
                        session,
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        default_warehouse_id=warehouse_id,
                        order_dto=order,
                    )
                    break
        else:
            return False
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id=f"shopify:{domain}",
            action=f"shopify.webhook.{topic}",
            resource_type="channel_account",
            resource_id=channel_account_id,
        )
    return True


async def process_shopify_webhook(
    receipt_id: str, *, http_client: httpx.AsyncClient | None = None
) -> int:
    """Drain one persisted Shopify delivery by its raw webhook receipt id."""
    from erp_core.channels.webhook_store import set_raw_webhook_processing_state
    from erp_core.shared.config import get_settings as load_settings
    from erp_core.shared.db import (
        create_migrator_engine,
        create_runtime_engine,
        create_session_factory,
    )

    settings = load_settings()
    runtime_engine = create_runtime_engine(settings)
    broker_engine = create_migrator_engine(settings)
    try:
        broker_factory = create_session_factory(broker_engine)
        runtime_factory = create_session_factory(runtime_engine)
        raw_id = UUID(receipt_id)
        async with system_session(broker_factory) as session:
            receipt = (
                await session.execute(
                    text(
                        "SELECT raw_payload, headers FROM raw_webhook_events "
                        "WHERE id = :id AND channel_type = 'shopify' AND signature_verified "
                        "AND processing_status IN ('received', 'queued') FOR UPDATE"
                    ),
                    {"id": raw_id},
                )
            ).one_or_none()
            if receipt is None:
                return 0
            headers = receipt.headers or {}
            topic = str(headers.get("x-shopify-topic") or "")
            domain = str(headers.get("x-shopify-shop-domain") or "")
            applied = await process_shopify_webhook_event(
                runtime_factory, topic, domain, dict(receipt.raw_payload or {}),
                http_client=http_client,
            )
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=raw_id,
                status="processed" if applied else "dead_letter",
                error_message=None if applied else "Unroutable Shopify delivery or topic",
            )
            return int(applied)
    finally:
        await runtime_engine.dispose()
        await broker_engine.dispose()
