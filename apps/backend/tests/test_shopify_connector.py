from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    register_channel_routing,
    resolve_routing_tenant,
)
from erp_core.channels.ecommerce.fulfillment_dispatcher import push_order_fulfillment
from erp_core.channels.ecommerce.shopify import (
    ORDERS_GRAPHQL_QUERY,
    SHOPIFY_API_PATH,
    ShopifyGraphqlAdapter,
    build_shopify_oauth_install_url,
    process_shopify_webhook_event,
    verify_shopify_webhook_hmac,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, UserRole


def _order_node() -> dict[str, object]:
    return {
        "id": "gid://shopify/Order/55001",
        "name": "#1001-B2B",
        "updatedAt": "2026-11-19T01:10:00Z",
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "UNFULFILLED",
        "currencyCode": "USD",
        "poNumber": "PO-IBERIA-2026-88",
        "currentSubtotalPriceSet": {"shopMoney": {"amount": "150.00", "currencyCode": "USD"}},
        "totalShippingPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
        "totalTaxSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
        "currentTotalPriceSet": {"shopMoney": {"amount": "170.00", "currencyCode": "USD"}},
        "customer": {
            "id": "gid://shopify/Customer/8801",
            "email": "b2b.buyer@nordiclights.se",
            "phone": "+46701234567",
            "firstName": "Sven",
            "lastName": "Lindqvist",
        },
        "shippingAddress": {"countryCodeV2": "SE"},
        "purchasingEntity": {
            "__typename": "PurchasingCompany",
            "company": {
                "id": "gid://shopify/Company/901",
                "name": "Nordic Architectural Lighting AB",
            },
            "location": {
                "id": "gid://shopify/CompanyLocation/902",
                "shippingAddress": {"countryCode": "SE"},
            },
        },
        "lineItems": {
            "nodes": [
                {
                    "sku": "SHOP-LED-5M-2PACK",
                    "title": "2-pack LED strip",
                    "quantity": 3,
                    "originalTotalSet": {"shopMoney": {"amount": "150.00", "currencyCode": "USD"}},
                }
            ]
        },
    }


async def test_shopify_graphql_b2b_bundle_token_refresh_and_gdpr(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, other_tenant, account_id, warehouse_id = (uuid4() for _ in range(4))
    domain = f"qz-led-{tenant_id.hex[:6]}.myshopify.com"
    other_domain = f"evil-{other_tenant.hex[:6]}.myshopify.com"
    client_secret = "shpss_test_secret_123"
    posts: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        if request.url.path == "/admin/oauth/access_token":
            assert json.loads(request.content)["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": "shpat_fresh", "expires_in": 86399})
        assert request.url.path == SHOPIFY_API_PATH
        assert request.headers["X-Shopify-Access-Token"] == "shpat_fresh"
        query = json.loads(request.content)["query"]
        if "fulfillmentCreate" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "fulfillmentCreate": {
                            "fulfillment": {
                                "id": "gid://shopify/Fulfillment/777",
                                "status": "SUCCESS",
                            },
                            "userErrors": [],
                        }
                    }
                },
            )
        connection = {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_order_node()],
        }
        return httpx.Response(
            200,
            json={
                "data": {"orders": connection} if "orders(" in query else {"products": connection},
                "extensions": {
                    "cost": {"throttleStatus": {"currentlyAvailable": 19950, "restoreRate": 1000}}
                },
            },
        )

    crypto = EnvelopeCryptoService(get_settings())
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Shop', :slug)"),
                {"tid": tenant_id, "slug": f"shp-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name) VALUES (:id, :tid, 'shopify', :ext, :ext)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": domain},
            )
            await register_channel_routing(
                session,
                channel_type="shopify",
                external_account_id=domain,
                tenant_id=tenant_id,
                channel_account_id=account_id,
            )
            await crypto.provision_tenant_dek(session, tenant_id)
            await CredentialVaultHelper(crypto).save_secret_json(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                credential_type="shopify_custom_app",
                payload={
                    "shop_domain": domain,
                    "auth_mode": "client_credentials",
                    "client_id": "cli",
                    "client_secret": client_secret,
                    "access_token": "shpat_stale",
                },
                expires_at=datetime.now(UTC) - timedelta(minutes=10),
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-SHP-5M", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("30.0000"),
                    retail_price_usd=Decimal("25.00"),
                    weight_grams=300,
                ),
            )
            await catalog.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=account_id,
                    external_product_id="gid://shopify/Product/1",
                    external_sku="SHOP-LED-5M-2PACK",
                    bundle_multiplier=2,
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                    "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 20, 0)"
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http,
            tenant_session(runtime_session_factory, tenant_id) as session,
        ):
            adapter = await ShopifyGraphqlAdapter.from_channel_account(
                session, tenant_id, account_id, crypto=crypto, http_client=http
            )
            orders, cursor = await adapter.fetch_orders_updated_since(
                datetime.now(UTC) - timedelta(hours=1)
            )
            assert cursor is None
            assert [r.url.path for r in posts if "oauth" in r.url.path] == [
                "/admin/oauth/access_token"
            ]
            assert all(r.url.path == SHOPIFY_API_PATH for r in posts if "oauth" not in r.url.path)
            assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
            assert orders[0].b2b_company is not None
            assert orders[0].b2b_company.legal_name == "Nordic Architectural Lighting AB"

            order_id, created = await EcommerceOrderSyncService().ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                default_warehouse_id=warehouse_id,
                order_dto=orders[0],
            )
            assert created is True
            again, created_again = await EcommerceOrderSyncService().ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                default_warehouse_id=warehouse_id,
                order_dto=orders[0],
            )
            assert (again, created_again) == (order_id, False)
            balance = await session.scalar(
                text(
                    "SELECT reserved_qty FROM inventory_balances WHERE tenant_id = :tid "
                    "AND warehouse_id = :wid AND variant_id = :vid"
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )
            assert balance == 6
            row = (
                await session.execute(
                    text(
                        "SELECT order_mode, payment_status, channel_metadata, "
                        "fulfillment_status "
                        "FROM orders WHERE id = :oid"
                    ),
                    {"oid": order_id},
                )
            ).one()
            assert (row.order_mode, row.payment_status, row.fulfillment_status) == (
                OrderMode.B2B_WHOLESALE.value,
                PaymentStatus.PAID.value,
                FulfillmentStatus.CONFIRMED_RESERVED.value,
            )
            assert row.channel_metadata["po_number"] == "PO-IBERIA-2026-88"

            fulfillment = await adapter.create_fulfillment(
                external_order_id="gid://shopify/Order/55001",
                fulfillment_order_id="gid://shopify/FulfillmentOrder/77",
                tracking_number="DHL-SE-9988",
                carrier_company="DHL",
            )
            assert fulfillment["fulfillment"]["status"] == "SUCCESS"

        body = json.dumps(
            {
                "shop_id": 12345,
                "shop_domain": domain,
                "customer": {"id": 8801, "email": "b2b.buyer@nordiclights.se"},
                "orders_to_redact": [55001],
            }
        ).encode()
        signature = base64.b64encode(
            hmac.new(client_secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        assert verify_shopify_webhook_hmac(body, signature, client_secret) is True
        assert verify_shopify_webhook_hmac(body + b" ", signature, client_secret) is False

        class RecordingPort:
            def __init__(self) -> None:
                self.calls: list[tuple[str, UUID]] = []

            async def handle_customer_data_request(self, session, tenant_id, payload) -> None:
                self.calls.append(("customers/data_request", tenant_id))

            async def handle_customer_redact(self, session, tenant_id, payload) -> None:
                self.calls.append(("customers/redact", tenant_id))
                await session.execute(
                    text(
                        "UPDATE customers SET display_name = 'REDACTED', primary_email = NULL, "
                        "primary_phone_e164 = NULL "
                        "WHERE tenant_id = :tid AND primary_email = :email"
                    ),
                    {"tid": tenant_id, "email": payload["customer"]["email"].lower()},
                )

            async def handle_shop_redact(self, session, tenant_id, payload) -> None:
                self.calls.append(("shop/redact", tenant_id))

        port = RecordingPort()
        payload = json.loads(body)
        assert (
            await process_shopify_webhook_event(
                runtime_session_factory, "customers/redact", domain, payload, gdpr_port=port
            )
            is True
        )
        assert port.calls == [("customers/redact", tenant_id)]
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            customer = (
                await session.execute(
                    text(
                        "SELECT display_name, primary_email FROM customers WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant_id},
                )
            ).one()
            assert (customer.display_name, customer.primary_email) == ("REDACTED", None)

        # shop/redact must deactivate routing so later deliveries cannot resolve the tenant.
        assert (
            await process_shopify_webhook_event(
                runtime_session_factory, "shop/redact", domain, payload, gdpr_port=port
            )
            is True
        )
        async with runtime_session_factory() as session:
            assert await resolve_routing_tenant(session, "shopify", domain) is None

        # A second tenant must never take over the first tenant's shop domain.
        async with tenant_session(runtime_session_factory, other_tenant) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Evil', :slug)"),
                {"tid": other_tenant, "slug": f"evil-{other_tenant.hex}"},
            )
            other_account = uuid4()
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name) VALUES (:id, :tid, 'shopify', :ext, :ext)"
                ),
                {"id": other_account, "tid": other_tenant, "ext": other_domain},
            )
            with pytest.raises(Exception, match="owned by another tenant"):
                await register_channel_routing(
                    session,
                    channel_type="shopify",
                    external_account_id=domain,
                    tenant_id=other_tenant,
                    channel_account_id=other_account,
                )
    finally:
        # Administrative cleanup (superuser) mirrors the other integration suites:
        # audit_events is immutable for app_runtime, so teardown needs the admin role.
        admin_engine = create_async_engine(_admin_url())
        try:
            async with admin_engine.begin() as conn:
                for statement in (
                    "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                    "DELETE FROM payment_records WHERE tenant_id = :tid",
                    "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                    "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                    "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                    "DELETE FROM order_items WHERE tenant_id = :tid",
                    "DELETE FROM orders WHERE tenant_id = :tid",
                    "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                    "DELETE FROM product_variants WHERE tenant_id = :tid",
                    "DELETE FROM products WHERE tenant_id = :tid",
                    "DELETE FROM customer_identities WHERE tenant_id = :tid",
                    "DELETE FROM customers WHERE tenant_id = :tid",
                    "DELETE FROM b2b_companies WHERE tenant_id = :tid",
                    "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                    "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                    "DELETE FROM tenant_keys WHERE tenant_id = :tid",
                    "DELETE FROM audit_events WHERE tenant_id = :tid",
                ):
                    for tenant in (tenant_id, other_tenant):
                        await conn.execute(text(statement), {"tid": tenant})
                await conn.execute(
                    text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                    {"a": tenant_id, "b": other_tenant},
                )
        finally:
            await admin_engine.dispose()


def test_shopify_orders_query_uses_company_address_country_code() -> None:
    """Shopify CompanyAddress exposes `countryCode`, whereas MailingAddress uses `countryCodeV2`."""
    assert "location { id name externalId shippingAddress { countryCode } }" in ORDERS_GRAPHQL_QUERY
    assert (
        "location { id name externalId shippingAddress { countryCodeV2 } }"
        not in ORDERS_GRAPHQL_QUERY
    )
    normalized = ShopifyGraphqlAdapter._normalize_order_node(_order_node())
    assert normalized.b2b_company is not None
    assert normalized.b2b_company.country_code == "SE"


def test_shopify_oauth_install_url_requests_fulfillment_order_scopes() -> None:
    url = build_shopify_oauth_install_url(
        "qzled.myshopify.com",
        "https://erp.example.com/callback",
        state="t1",
        client_id="cid_test",
    )
    assert "read_merchant_managed_fulfillment_orders" in url
    assert "write_merchant_managed_fulfillment_orders" in url


@pytest.mark.asyncio
async def test_shopify_updated_since_quotes_iso_timestamp() -> None:
    """Shopify search queries require single quotes around ISO timestamps containing colons."""
    assert "reverse: true" in ORDERS_GRAPHQL_QUERY

    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        q = str((body.get("variables") or {}).get("query") or "")
        seen_queries.append(q)
        gql = str(body.get("query") or "")
        if "SyncOrders" in gql:
            return httpx.Response(
                200,
                json={"data": {"orders": {"pageInfo": {"hasNextPage": False}, "nodes": []}}},
            )
        if "SyncProducts" in gql:
            return httpx.Response(
                200,
                json={"data": {"products": {"pageInfo": {"hasNextPage": False}, "nodes": []}}},
            )
        raise AssertionError(f"Unexpected GraphQL query: {gql}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ShopifyGraphqlAdapter(
        shop_domain="qzled.myshopify.com",
        access_token="shpat_test_quote",
        http_client=client,
    )
    since = datetime(2026, 11, 25, 3, 0, 0, tzinfo=UTC)
    try:
        await adapter.fetch_orders_updated_since(since)
        await adapter.fetch_products_updated_since(since)
    finally:
        await client.aclose()

    assert seen_queries == [
        "updated_at:>='2026-11-25T03:00:00Z'",
        "updated_at:>='2026-11-25T03:00:00Z'",
    ]


def _admin_url() -> str:
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


def test_shopify_orders_query_includes_refund_and_cancel_fields() -> None:
    assert "cancelledAt" in ORDERS_GRAPHQL_QUERY
    assert "totalRefundedSet" in ORDERS_GRAPHQL_QUERY


async def _cleanup_test_tenant(tenant_id: UUID) -> None:
    admin_engine = create_async_engine(_admin_url())
    try:
        async with admin_engine.begin() as conn:
            for statement in (
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM payment_records WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM shipments WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM customer_identities WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
                "DELETE FROM b2b_companies WHERE tenant_id = :tid",
                "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM tenant_keys WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
                "DELETE FROM tenants WHERE id = :tid",
            ):
                await conn.execute(text(statement), {"tid": tenant_id})
    finally:
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_shopify_partial_refund_and_cancellation_webhooks_update_inventory_and_profit(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, warehouse_id = uuid4(), uuid4(), uuid4()
    domain = f"refund-{tenant_id.hex[:6]}.myshopify.com"
    order_state: dict[str, object] = {
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "UNFULFILLED",
        "cancelledAt": None,
        "refunded": "0.00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == SHOPIFY_API_PATH
        node = {
            "id": "gid://shopify/Order/99001",
            "name": "#1003",
            "updatedAt": "2026-11-25T07:30:00Z",
            "cancelledAt": order_state["cancelledAt"],
            "displayFinancialStatus": order_state["displayFinancialStatus"],
            "displayFulfillmentStatus": order_state["displayFulfillmentStatus"],
            "currencyCode": "USD",
            "currentSubtotalPriceSet": {"shopMoney": {"amount": "33.80", "currencyCode": "USD"}},
            "totalShippingPriceSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
            "totalTaxSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
            "currentTotalPriceSet": {"shopMoney": {"amount": "33.80", "currencyCode": "USD"}},
            "totalRefundedSet": {
                "shopMoney": {"amount": str(order_state["refunded"]), "currencyCode": "USD"}
            },
            "customer": {
                "id": "gid://shopify/Customer/9901",
                "email": "buyer@example.com",
                "firstName": "Alex",
                "lastName": "Buyer",
            },
            "shippingAddress": {"countryCodeV2": "US"},
            "lineItems": {
                "nodes": [
                    {
                        "sku": "SHOP-UFO-100W",
                        "title": "UFO High Bay 100W",
                        "quantity": 2,
                        "originalTotalSet": {
                            "shopMoney": {"amount": "33.80", "currencyCode": "USD"}
                        },
                    }
                ]
            },
        }
        return httpx.Response(
            200,
            json={"data": {"orders": {"pageInfo": {"hasNextPage": False}, "nodes": [node]}}},
        )

    crypto = EnvelopeCryptoService(get_settings())
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, base_currency) "
                    "VALUES (:tid, 'Refund Shop', :slug, 'USD')"
                ),
                {"tid": tenant_id, "slug": f"ref-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, config) "
                    "VALUES (:id, :tid, 'shopify', :ext, :ext, CAST(:cfg AS jsonb))"
                ),
                {
                    "id": account_id,
                    "tid": tenant_id,
                    "ext": domain,
                    "cfg": json.dumps({"warehouse_id": str(warehouse_id)}),
                },
            )
            await register_channel_routing(
                session,
                channel_type="shopify",
                external_account_id=domain,
                tenant_id=tenant_id,
                channel_account_id=account_id,
            )
            await crypto.provision_tenant_dek(session, tenant_id)
            await CredentialVaultHelper(crypto).save_secret_json(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                credential_type="shopify_custom_app",
                payload={"shop_domain": domain, "auth_mode": "oauth", "access_token": "shpat_ref"},
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-UFO-REF", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="UFO-100W-REF",
                    cost_price_base=Decimal("9.4444"),
                    retail_price_usd=Decimal("16.90"),
                    weight_grams=500,
                ),
            )
            await catalog.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=account_id,
                    external_product_id="gid://shopify/Product/99",
                    external_sku="SHOP-UFO-100W",
                    bundle_multiplier=1,
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                    "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 86, 0)"
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert (
                await process_shopify_webhook_event(
                    runtime_session_factory,
                    "orders/create",
                    domain,
                    {"id": 99001, "admin_graphql_api_id": "gid://shopify/Order/99001"},
                    http_client=http,
                )
                is True
            )

            order_state["displayFinancialStatus"] = "PARTIALLY_REFUNDED"
            order_state["refunded"] = "16.90"
            assert (
                await process_shopify_webhook_event(
                    runtime_session_factory,
                    "refunds/create",
                    domain,
                    {"id": 444, "order_id": 99001},
                    http_client=http,
                )
                is True
            )

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            partial_row = (
                await session.execute(
                    text(
                        "SELECT o.payment_status, o.fulfillment_status, o.channel_metadata, "
                        "ops.gross_revenue_base, ops.cogs_base, ops.net_contribution_margin_base, "
                        "ib.reserved_qty "
                        "FROM orders o "
                        "JOIN order_profit_snapshots ops ON ops.order_id = o.id "
                        "JOIN inventory_balances ib ON ib.variant_id = :vid "
                        "WHERE o.external_order_id = 'gid://shopify/Order/99001'"
                    ),
                    {"vid": variant.id},
                )
            ).one()
            assert partial_row.payment_status == PaymentStatus.PAID.value
            assert partial_row.fulfillment_status == FulfillmentStatus.CONFIRMED_RESERVED.value
            assert partial_row.channel_metadata["refunded_amount"] == "16.90"
            assert Decimal(str(partial_row.gross_revenue_base)) == Decimal("16.90")
            assert Decimal(str(partial_row.cogs_base)) == Decimal("18.89")
            assert partial_row.reserved_qty == 2

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            order_state["displayFinancialStatus"] = "REFUNDED"
            order_state["cancelledAt"] = "2026-11-25T08:00:00Z"
            order_state["refunded"] = "33.80"
            assert (
                await process_shopify_webhook_event(
                    runtime_session_factory,
                    "orders/cancelled",
                    domain,
                    {"id": 99001, "admin_graphql_api_id": "gid://shopify/Order/99001"},
                    http_client=http,
                )
                is True
            )

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            cancelled_row = (
                await session.execute(
                    text(
                        "SELECT o.payment_status, o.fulfillment_status, o.channel_metadata, "
                        "ops.gross_revenue_base, ops.cogs_base, ops.net_contribution_margin_base, "
                        "ib.reserved_qty "
                        "FROM orders o "
                        "JOIN order_profit_snapshots ops ON ops.order_id = o.id "
                        "JOIN inventory_balances ib ON ib.variant_id = :vid "
                        "WHERE o.external_order_id = 'gid://shopify/Order/99001'"
                    ),
                    {"vid": variant.id},
                )
            ).one()
            assert cancelled_row.payment_status == PaymentStatus.REFUNDED.value
            assert cancelled_row.fulfillment_status == FulfillmentStatus.CANCELLED.value
            assert cancelled_row.channel_metadata["cancelled_at"] == "2026-11-25T08:00:00Z"
            assert Decimal(str(cancelled_row.gross_revenue_base)) == Decimal("0.00")
            assert Decimal(str(cancelled_row.cogs_base)) == Decimal("0.00")
            assert Decimal(str(cancelled_row.net_contribution_margin_base)) == Decimal("0.00")
            assert cancelled_row.reserved_qty == 0
    finally:
        await _cleanup_test_tenant(tenant_id)


@pytest.mark.asyncio
async def test_shopify_fulfillment_writeback_resolves_fulfillment_order_and_persists_status(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, warehouse_id = uuid4(), uuid4(), uuid4()
    domain = f"fulfill-{tenant_id.hex[:6]}.myshopify.com"
    recorded_mutations: list[dict[str, object]] = []
    fail_mode = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        gql = str(body.get("query") or "")
        variables = body.get("variables") or {}
        if "OrderFulfillmentOrders" in gql:
            assert variables["id"] == "gid://shopify/Order/99002"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "order": {
                            "fulfillmentOrders": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/FulfillmentOrder/8800",
                                        "status": "CLOSED",
                                        "supportedActions": [],
                                    },
                                    {
                                        "id": "gid://shopify/FulfillmentOrder/8801",
                                        "status": "OPEN",
                                        "supportedActions": [{"action": "CREATE_FULFILLMENT"}],
                                    },
                                ]
                            }
                        }
                    }
                },
            )
        if "fulfillmentCreate" in gql:
            recorded_mutations.append(variables)
            if fail_mode["fail"]:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "fulfillmentCreate": {
                                "fulfillment": None,
                                "userErrors": [{"field": ["fulfillment"], "message": "Denied"}],
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "fulfillmentCreate": {
                            "fulfillment": {
                                "id": "gid://shopify/Fulfillment/9901",
                                "status": "SUCCESS",
                                "trackingInfo": [
                                    {"company": "DHL", "number": "DHL-99002", "url": None}
                                ],
                            },
                            "userErrors": [],
                        }
                    }
                },
            )
        if "SyncOrders" in gql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "orders": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {
                                    "id": "gid://shopify/Order/99002",
                                    "name": "#1004",
                                    "updatedAt": "2026-11-25T07:45:00Z",
                                    "displayFinancialStatus": "PAID",
                                    "displayFulfillmentStatus": "UNFULFILLED",
                                    "currencyCode": "USD",
                                    "currentSubtotalPriceSet": {
                                        "shopMoney": {"amount": "16.90", "currencyCode": "USD"}
                                    },
                                    "totalShippingPriceSet": {
                                        "shopMoney": {"amount": "0.00", "currencyCode": "USD"}
                                    },
                                    "totalTaxSet": {
                                        "shopMoney": {"amount": "0.00", "currencyCode": "USD"}
                                    },
                                    "currentTotalPriceSet": {
                                        "shopMoney": {"amount": "16.90", "currencyCode": "USD"}
                                    },
                                    "customer": {
                                        "id": "gid://shopify/Customer/9902",
                                        "email": "ship@example.com",
                                    },
                                    "shippingAddress": {"countryCodeV2": "US"},
                                    "lineItems": {
                                        "nodes": [
                                            {
                                                "sku": "SHOP-UFO-SHIP",
                                                "title": "UFO High Bay",
                                                "quantity": 1,
                                                "originalTotalSet": {
                                                    "shopMoney": {
                                                        "amount": "16.90",
                                                        "currencyCode": "USD",
                                                    }
                                                },
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    }
                },
            )
        raise AssertionError(f"Unexpected query: {gql}")

    crypto = EnvelopeCryptoService(get_settings())
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, base_currency) "
                    "VALUES (:tid, 'Fulfill Shop', :slug, 'USD')"
                ),
                {"tid": tenant_id, "slug": f"ful-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, config) "
                    "VALUES (:id, :tid, 'shopify', :ext, :ext, CAST(:cfg AS jsonb))"
                ),
                {
                    "id": account_id,
                    "tid": tenant_id,
                    "ext": domain,
                    "cfg": json.dumps({"warehouse_id": str(warehouse_id)}),
                },
            )
            await register_channel_routing(
                session,
                channel_type="shopify",
                external_account_id=domain,
                tenant_id=tenant_id,
                channel_account_id=account_id,
            )
            await crypto.provision_tenant_dek(session, tenant_id)
            await CredentialVaultHelper(crypto).save_secret_json(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                credential_type="shopify_custom_app",
                payload={"shop_domain": domain, "auth_mode": "oauth", "access_token": "shpat_ful"},
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-UFO-FUL", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="UFO-100W-FUL",
                    cost_price_base=Decimal("9.4444"),
                    retail_price_usd=Decimal("16.90"),
                    weight_grams=500,
                ),
            )
            await catalog.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=account_id,
                    external_product_id="gid://shopify/Product/100",
                    external_sku="SHOP-UFO-SHIP",
                    bundle_multiplier=1,
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                    "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 20, 0)"
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            tenant_session(runtime_session_factory, tenant_id) as session,
        ):
            adapter = await ShopifyGraphqlAdapter.from_channel_account(
                session, tenant_id, account_id, crypto=crypto, http_client=http
            )
            orders, _ = await adapter.fetch_orders_updated_since(
                datetime.now(UTC) - timedelta(hours=1)
            )
            order_id, created = await EcommerceOrderSyncService().ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                default_warehouse_id=warehouse_id,
                order_dto=orders[0],
            )
            assert created is True

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            tenant_session(runtime_session_factory, tenant_id) as session,
        ):
            res = await push_order_fulfillment(
                session,
                tenant_id,
                order_id,
                tracking_number="DHL-99002",
                carrier_company="DHL",
                raise_on_error=False,
                http_client=http,
            )
            assert res is not None
            assert res["status"] == "synced"
            assert res["external_fulfillment_id"] == "gid://shopify/Fulfillment/9901"

        assert recorded_mutations[0]["fulfillment"]["lineItemsByFulfillmentOrder"] == [
            {"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/8801"}
        ]

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            meta = await session.scalar(
                text("SELECT channel_metadata FROM orders WHERE id = :oid"),
                {"oid": order_id},
            )
            assert meta["external_fulfillment"]["status"] == "synced"
            assert meta["external_fulfillment"]["external_fulfillment_id"] == (
                "gid://shopify/Fulfillment/9901"
            )

        fail_mode["fail"] = True
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            tenant_session(runtime_session_factory, tenant_id) as session,
        ):
            fail_res = await push_order_fulfillment(
                session,
                tenant_id,
                order_id,
                tracking_number="DHL-99002",
                carrier_company="DHL",
                raise_on_error=False,
                http_client=http,
            )
            assert fail_res is not None
            assert fail_res["status"] == "failed"
            assert fail_res["error_code"] == "ERR_SHOPIFY_FULFILLMENT"

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            meta2 = await session.scalar(
                text("SELECT channel_metadata FROM orders WHERE id = :oid"),
                {"oid": order_id},
            )
            assert meta2["external_fulfillment"]["status"] == "failed"
    finally:
        await _cleanup_test_tenant(tenant_id)
