"""Concrete live-push adapter for the catalog's ``MarketplaceListingPublisher`` protocol.

``erp_core.catalog`` must never import ``erp_core.channels`` (import-linter contract
``catalog_isolated``), so this module lives on the channel side of the boundary and is
injected into a ``SourcingService.publish(..., listing_publisher=...)`` call by the
composition root (``api_main``) or by a test.

Contract:

* Channel credentials are read from the envelope-encrypted vault
  (``AAD = f"{tenant_id}:{channel_account_id}:{cred_type}"``) and never leave this module.
* Only channels with a real publish implementation are supported (the 16 live
  marketplaces listed in :data:`SUPPORTED_CHANNEL_TYPES`); anything else fails closed
  with ``ERR_MARKETPLACE_LIVE_PUSH_UNSUPPORTED`` instead of faking a live listing.
* A remote rejection propagates the adapter's typed ``AppError`` unchanged, so the caller
  can never persist a ``live_published`` state for a listing the marketplace refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.sourcing_service import (
    MarketplaceListingDispatchReceipt,
    MarketplaceListingPayload,
    MarketplaceVariantReceipt,
)
from erp_core.channels.ecommerce.alibaba_intl import AlibabaIntlAdapter
from erp_core.channels.ecommerce.aliexpress import AliExpressAdapter
from erp_core.channels.ecommerce.amazon_sp import AmazonSpApiAdapter
from erp_core.channels.ecommerce.base import CommercePlatformAdapter, CredentialVaultHelper
from erp_core.channels.ecommerce.ebay import EbayConnectorAdapter
from erp_core.channels.ecommerce.lazada import LazadaAdapter
from erp_core.channels.ecommerce.mercado_libre import MercadoLibreAdapter
from erp_core.channels.ecommerce.ozon import OzonAdapter
from erp_core.channels.ecommerce.shein import SheinAdapter
from erp_core.channels.ecommerce.shopee import ShopeeAdapter
from erp_core.channels.ecommerce.shopify import ShopifyGraphqlAdapter
from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
from erp_core.channels.ecommerce.shopline import ShoplineAdapter
from erp_core.channels.ecommerce.temu import TemuAdapter
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.channels.ecommerce.walmart import WalmartAdapter
from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter
from erp_core.shared.errors import AppError

logger = structlog.get_logger(__name__)

AMAZON_SP_CHANNEL_TYPE = "amazon_sp"
EBAY_CHANNEL_TYPE = "ebay"
ALIBABA_INTL_CHANNEL_TYPE = "alibaba_email"
SHOPIFY_CHANNEL_TYPE = "shopify"
WOOCOMMERCE_CHANNEL_TYPE = "woocommerce"
SHOPLINE_CHANNEL_TYPE = "shopline"
SHOPLAZZA_CHANNEL_TYPE = "shoplazza"
TIKTOK_SHOP_CHANNEL_TYPE = "tiktok_shop"
SHOPEE_CHANNEL_TYPE = "shopee"
LAZADA_CHANNEL_TYPE = "lazada"
TEMU_CHANNEL_TYPE = "temu"
SHEIN_CHANNEL_TYPE = "shein"
ALIEXPRESS_CHANNEL_TYPE = "aliexpress"
OZON_CHANNEL_TYPE = "ozon"
WALMART_CHANNEL_TYPE = "walmart"
MERCADO_LIBRE_CHANNEL_TYPE = "mercado_libre"

AMAZON_CREDENTIAL_TYPE = "amazon_sp_api"
EBAY_CREDENTIAL_TYPE = "ebay_api"
ALIBABA_INTL_CREDENTIAL_TYPE = "alibaba_intl_app"
SHOPIFY_CREDENTIAL_TYPE = "shopify_custom_app"
SHOPIFY_FALLBACK_CREDENTIAL_TYPE = "oauth_token"
WOOCOMMERCE_CREDENTIAL_TYPE = "woocommerce_keys"
SHOPLINE_CREDENTIAL_TYPE = "shopline_app"
SHOPLAZZA_CREDENTIAL_TYPE = "shoplazza_app"
TIKTOK_SHOP_CREDENTIAL_TYPE = "tiktok_shop_app"
SHOPEE_CREDENTIAL_TYPE = "shopee_app"
LAZADA_CREDENTIAL_TYPE = "lazada_app"
TEMU_CREDENTIAL_TYPE = "temu_app"
SHEIN_CREDENTIAL_TYPE = "shein_app"
ALIEXPRESS_CREDENTIAL_TYPE = "aliexpress_app"
OZON_CREDENTIAL_TYPE = "ozon_app"
WALMART_CREDENTIAL_TYPE = "walmart_app"
MERCADO_LIBRE_CREDENTIAL_TYPE = "mercado_libre_app"

DEFAULT_AMAZON_MARKETPLACE_ID = "ATVPDKIKX0DER"
DEFAULT_EBAY_SITE_ID = "EBAY_US"

#: Region/fulfilment defaults mirror each adapter's own constructor default, so a credential
#: blob that omits them builds exactly the adapter the adapter class would build.
DEFAULT_SHOPEE_REGION = "GLOBAL"
DEFAULT_LAZADA_REGION = "SG"
DEFAULT_TEMU_REGION = "US"
DEFAULT_SHEIN_REGION = "US"
DEFAULT_MERCADO_LIBRE_SITE_ID = "MLM"
DEFAULT_FULFILLMENT_MODE = "standard"
DEFAULT_OZON_FULFILLMENT_MODE = "fbs"

#: Marketplace channels that can actually be pushed to a live API today.
SUPPORTED_CHANNEL_TYPES: frozenset[str] = frozenset(
    {
        AMAZON_SP_CHANNEL_TYPE,
        EBAY_CHANNEL_TYPE,
        ALIBABA_INTL_CHANNEL_TYPE,
        SHOPIFY_CHANNEL_TYPE,
        WOOCOMMERCE_CHANNEL_TYPE,
        SHOPLINE_CHANNEL_TYPE,
        SHOPLAZZA_CHANNEL_TYPE,
        TIKTOK_SHOP_CHANNEL_TYPE,
        SHOPEE_CHANNEL_TYPE,
        LAZADA_CHANNEL_TYPE,
        TEMU_CHANNEL_TYPE,
        SHEIN_CHANNEL_TYPE,
        ALIEXPRESS_CHANNEL_TYPE,
        OZON_CHANNEL_TYPE,
        WALMART_CHANNEL_TYPE,
        MERCADO_LIBRE_CHANNEL_TYPE,
    }
)

#: ``channel_accounts.channel_type`` -> ``channel_credentials.credential_type``.
CREDENTIAL_TYPES: dict[str, str] = {
    AMAZON_SP_CHANNEL_TYPE: AMAZON_CREDENTIAL_TYPE,
    EBAY_CHANNEL_TYPE: EBAY_CREDENTIAL_TYPE,
    ALIBABA_INTL_CHANNEL_TYPE: ALIBABA_INTL_CREDENTIAL_TYPE,
    SHOPIFY_CHANNEL_TYPE: SHOPIFY_CREDENTIAL_TYPE,
    WOOCOMMERCE_CHANNEL_TYPE: WOOCOMMERCE_CREDENTIAL_TYPE,
    SHOPLINE_CHANNEL_TYPE: SHOPLINE_CREDENTIAL_TYPE,
    SHOPLAZZA_CHANNEL_TYPE: SHOPLAZZA_CREDENTIAL_TYPE,
    TIKTOK_SHOP_CHANNEL_TYPE: TIKTOK_SHOP_CREDENTIAL_TYPE,
    SHOPEE_CHANNEL_TYPE: SHOPEE_CREDENTIAL_TYPE,
    LAZADA_CHANNEL_TYPE: LAZADA_CREDENTIAL_TYPE,
    TEMU_CHANNEL_TYPE: TEMU_CREDENTIAL_TYPE,
    SHEIN_CHANNEL_TYPE: SHEIN_CREDENTIAL_TYPE,
    ALIEXPRESS_CHANNEL_TYPE: ALIEXPRESS_CREDENTIAL_TYPE,
    OZON_CHANNEL_TYPE: OZON_CREDENTIAL_TYPE,
    WALMART_CHANNEL_TYPE: WALMART_CREDENTIAL_TYPE,
    MERCADO_LIBRE_CHANNEL_TYPE: MERCADO_LIBRE_CREDENTIAL_TYPE,
}

#: Keys every decrypted credential blob must carry before an adapter is built.
REQUIRED_CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    AMAZON_SP_CHANNEL_TYPE: (
        "seller_id",
        "lwa_client_id",
        "lwa_client_secret",
        "refresh_token",
    ),
    EBAY_CHANNEL_TYPE: ("seller_username", "app_id", "cert_id", "refresh_token"),
    ALIBABA_INTL_CHANNEL_TYPE: ("store_id", "app_key", "app_secret"),
    SHOPIFY_CHANNEL_TYPE: ("shop_domain",),
    WOOCOMMERCE_CHANNEL_TYPE: ("store_url", "consumer_key", "consumer_secret"),
    SHOPLINE_CHANNEL_TYPE: ("store_domain", "access_token"),
    SHOPLAZZA_CHANNEL_TYPE: ("store_domain", "access_token"),
    TIKTOK_SHOP_CHANNEL_TYPE: ("app_key", "app_secret", "access_token", "shop_cipher"),
    SHOPEE_CHANNEL_TYPE: ("shop_id", "partner_id", "partner_key", "access_token"),
    LAZADA_CHANNEL_TYPE: ("seller_id", "app_key", "app_secret", "access_token"),
    TEMU_CHANNEL_TYPE: ("mall_id", "app_key", "app_secret", "access_token"),
    SHEIN_CHANNEL_TYPE: ("seller_id", "open_key_id", "secret_key"),
    ALIEXPRESS_CHANNEL_TYPE: ("seller_id", "app_key", "app_secret", "access_token"),
    OZON_CHANNEL_TYPE: ("client_id", "api_key"),
    WALMART_CHANNEL_TYPE: ("seller_id", "client_id", "client_secret"),
    MERCADO_LIBRE_CHANNEL_TYPE: ("seller_id", "access_token", "site_id"),
}


def _text(credentials: Mapping[str, Any], key: str, default: str = "") -> str:
    value = credentials.get(key)
    return str(value).strip() if value not in (None, "") else default


def _sandbox(credentials: Mapping[str, Any]) -> bool:
    raw = credentials.get("sandbox")
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _invalid_credentials(channel_type: str, missing: tuple[str, ...]) -> AppError:
    return AppError(
        slug="marketplace-credentials-invalid",
        status_code=502,
        code="ERR_MARKETPLACE_CREDENTIALS_INVALID",
        title="Marketplace Credentials Invalid",
        detail=(
            f"The stored '{channel_type}' credential is missing required field(s): "
            f"{', '.join(sorted(missing))}."
        ),
    )


def unsupported_channel_error(channel_type: str) -> AppError:
    """Honest 422 for a channel that has no live marketplace publish API."""
    return AppError(
        slug="marketplace-live-push-unsupported",
        status_code=422,
        code="ERR_MARKETPLACE_LIVE_PUSH_UNSUPPORTED",
        title="Marketplace Live Push Unsupported",
        detail=(
            f"Channel type '{channel_type}' has no live marketplace API integration; "
            "supported channels are "
            f"{', '.join(sorted(SUPPORTED_CHANNEL_TYPES))}."
        ),
    )


def build_marketplace_adapter(
    channel_type: str,
    credentials: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    expires_at: Any = None,
    tenant_id: UUID | None = None,
    channel_account_id: UUID | None = None,
    vault: CredentialVaultHelper | None = None,
    session: AsyncSession | None = None,
) -> CommercePlatformAdapter | None:
    """Instantiate the real adapter for one marketplace channel from decrypted credentials.

    Returns ``None`` when ``channel_type`` has no live adapter, letting callers decide
    between a typed 422 (publish/sync endpoints) and their own fallback. A credential blob
    that is missing required fields raises ``ERR_MARKETPLACE_CREDENTIALS_INVALID`` instead
    of building an adapter that would fail with an opaque transport error later.
    """
    normalized = channel_type.strip().lower()
    if normalized not in SUPPORTED_CHANNEL_TYPES:
        return None
    missing = tuple(
        key for key in REQUIRED_CREDENTIAL_KEYS[normalized] if not _text(credentials, key)
    )
    if missing:
        raise _invalid_credentials(normalized, missing)

    if normalized == SHOPIFY_CHANNEL_TYPE:
        access_token = _text(credentials, "access_token")
        client_id = _text(credentials, "client_id") or None
        client_secret = _text(credentials, "client_secret") or None
        if not access_token and not (client_id and client_secret):
            raise _invalid_credentials(normalized, ("access_token",))
        auth_mode = _text(credentials, "auth_mode") or (
            "client_credentials" if client_id and client_secret and not access_token else "oauth"
        )
        return ShopifyGraphqlAdapter(
            shop_domain=_text(credentials, "shop_domain"),
            access_token=access_token,
            webhook_secret=client_secret or "",
            auth_mode=auth_mode,
            client_id=client_id,
            client_secret=client_secret,
            expires_at=expires_at,
            transport=transport,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            vault=vault,
            session=session,
        )

    if normalized == AMAZON_SP_CHANNEL_TYPE:
        return AmazonSpApiAdapter(
            seller_id=_text(credentials, "seller_id"),
            marketplace_id=_text(
                credentials, "marketplace_id", DEFAULT_AMAZON_MARKETPLACE_ID
            ).upper(),
            lwa_client_id=_text(credentials, "lwa_client_id")
            or _text(credentials, "client_id"),
            lwa_client_secret=_text(credentials, "lwa_client_secret")
            or _text(credentials, "client_secret"),
            refresh_token=_text(credentials, "refresh_token"),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == EBAY_CHANNEL_TYPE:
        return EbayConnectorAdapter(
            seller_username=_text(credentials, "seller_username"),
            site_id=_text(credentials, "site_id", DEFAULT_EBAY_SITE_ID).upper(),
            app_id=_text(credentials, "app_id"),
            cert_id=_text(credentials, "cert_id"),
            refresh_token=_text(credentials, "refresh_token"),
            sandbox=_sandbox(credentials),
            transport=transport,
        )
    if normalized == WOOCOMMERCE_CHANNEL_TYPE:
        return WooCommerceAdapter(
            store_url=_text(credentials, "store_url"),
            consumer_key=_text(credentials, "consumer_key"),
            consumer_secret=_text(credentials, "consumer_secret"),
            webhook_secret=_text(credentials, "webhook_secret"),
            wp_username=_text(credentials, "wp_username"),
            wp_app_password=_text(credentials, "wp_app_password"),
            transport=transport,
        )
    if normalized == SHOPLINE_CHANNEL_TYPE:
        return ShoplineAdapter(
            store_domain=_text(credentials, "store_domain"),
            access_token=_text(credentials, "access_token"),
            webhook_secret=_text(credentials, "webhook_secret"),
            transport=transport,
        )
    if normalized == SHOPLAZZA_CHANNEL_TYPE:
        return ShoplazzaAdapter(
            store_domain=_text(credentials, "store_domain"),
            access_token=_text(credentials, "access_token"),
            webhook_secret=_text(credentials, "client_secret"),
            transport=transport,
        )
    if normalized == TIKTOK_SHOP_CHANNEL_TYPE:
        return TikTokShopCoreAdapter(
            app_key=_text(credentials, "app_key"),
            app_secret=_text(credentials, "app_secret"),
            access_token=_text(credentials, "access_token"),
            shop_cipher=_text(credentials, "shop_cipher"),
            app_mode=_text(credentials, "app_mode", "seller_byok"),
            webhook_secret=_text(credentials, "webhook_secret") or _text(credentials, "app_secret"),
            warehouse_id=_text(credentials, "warehouse_id") or None,
            transport=transport,
        )
    if normalized == SHOPEE_CHANNEL_TYPE:
        return ShopeeAdapter(
            shop_id=_text(credentials, "shop_id"),
            partner_id=_text(credentials, "partner_id"),
            partner_key=_text(credentials, "partner_key"),
            access_token=_text(credentials, "access_token"),
            region=_text(credentials, "region", DEFAULT_SHOPEE_REGION).upper(),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == LAZADA_CHANNEL_TYPE:
        return LazadaAdapter(
            seller_id=_text(credentials, "seller_id"),
            app_key=_text(credentials, "app_key"),
            app_secret=_text(credentials, "app_secret"),
            access_token=_text(credentials, "access_token"),
            region=_text(credentials, "region", DEFAULT_LAZADA_REGION).upper(),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == TEMU_CHANNEL_TYPE:
        return TemuAdapter(
            mall_id=_text(credentials, "mall_id"),
            app_key=_text(credentials, "app_key"),
            app_secret=_text(credentials, "app_secret"),
            access_token=_text(credentials, "access_token"),
            region=_text(credentials, "region", DEFAULT_TEMU_REGION).upper(),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == SHEIN_CHANNEL_TYPE:
        return SheinAdapter(
            seller_id=_text(credentials, "seller_id"),
            open_key_id=_text(credentials, "open_key_id"),
            secret_key=_text(credentials, "secret_key"),
            region=_text(credentials, "region", DEFAULT_SHEIN_REGION).upper(),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == ALIEXPRESS_CHANNEL_TYPE:
        return AliExpressAdapter(
            seller_id=_text(credentials, "seller_id"),
            app_key=_text(credentials, "app_key"),
            app_secret=_text(credentials, "app_secret"),
            access_token=_text(credentials, "access_token"),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == OZON_CHANNEL_TYPE:
        return OzonAdapter(
            client_id=_text(credentials, "client_id"),
            api_key=_text(credentials, "api_key"),
            warehouse_id=_text(credentials, "warehouse_id"),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_OZON_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == WALMART_CHANNEL_TYPE:
        return WalmartAdapter(
            seller_id=_text(credentials, "seller_id"),
            client_id=_text(credentials, "client_id"),
            client_secret=_text(credentials, "client_secret"),
            access_token=_text(credentials, "access_token") or None,
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    if normalized == MERCADO_LIBRE_CHANNEL_TYPE:
        return MercadoLibreAdapter(
            seller_id=_text(credentials, "seller_id"),
            access_token=_text(credentials, "access_token"),
            site_id=_text(
                credentials, "site_id", DEFAULT_MERCADO_LIBRE_SITE_ID
            ).upper(),
            refresh_token=_text(credentials, "refresh_token"),
            fulfillment_mode=_text(
                credentials, "fulfillment_mode", DEFAULT_FULFILLMENT_MODE
            ).lower(),
            endpoint_url=_text(credentials, "endpoint_url") or None,
            transport=transport,
        )
    return AlibabaIntlAdapter(
        store_id=_text(credentials, "store_id"),
        app_key=_text(credentials, "app_key"),
        app_secret=_text(credentials, "app_secret"),
        access_token=_text(credentials, "access_token") or None,
        transport=transport,
    )


async def load_channel_credentials(
    vault: CredentialVaultHelper,
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    channel_type: str,
    primary_credential_type: str,
) -> tuple[Mapping[str, Any], Any]:
    """Load channel credentials from envelope vault, supporting Shopify legacy fallback."""
    try:
        return await vault.load_secret_json(
            session, tenant_id, channel_account_id, primary_credential_type
        )
    except AppError as exc:
        if channel_type == SHOPIFY_CHANNEL_TYPE and exc.code == "ERR_CREDENTIAL_NOT_FOUND":
            return await vault.load_secret_json(
                session,
                tenant_id,
                channel_account_id,
                SHOPIFY_FALLBACK_CREDENTIAL_TYPE,
            )
        raise


def _coerce_variant_receipts(
    raw_receipts: Any, *, fallback_sku: str
) -> list[MarketplaceVariantReceipt]:
    """Normalize a receipt's per-variant payload onto the catalog DTO.

    The global marketplaces already emit ``MarketplaceVariantReceipt`` objects, while the
    storefront connectors emit plain mappings; both must land in the same typed list so a
    caller never depends on which family built the listing.
    """
    coerced: list[MarketplaceVariantReceipt] = []
    for raw in raw_receipts or []:
        if isinstance(raw, MarketplaceVariantReceipt):
            coerced.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        coerced.append(
            MarketplaceVariantReceipt(
                external_sku=str(raw.get("external_sku") or fallback_sku),
                external_variant_id=raw.get("external_variant_id"),
                external_inventory_item_id=raw.get("external_inventory_item_id"),
            )
        )
    return coerced


def _normalize_payload_receipt(
    receipt: MarketplaceListingDispatchReceipt,
) -> MarketplaceListingDispatchReceipt:
    """Return the marketplace receipt with its variant receipts coerced to the DTO."""
    variant_receipts = _coerce_variant_receipts(
        getattr(receipt, "variant_receipts", None), fallback_sku=receipt.external_sku
    )
    return receipt.model_copy(update={"variant_receipts": variant_receipts})


class MarketplaceListingDispatcher:
    """Push one sourcing draft listing to a live marketplace API.

    Structurally implements ``erp_core.catalog.sourcing_service.MarketplaceListingPublisher``
    without the catalog module ever importing this file.
    """

    def __init__(
        self,
        *,
        credential_vault: CredentialVaultHelper | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._vault = credential_vault or CredentialVaultHelper()
        #: Test seam: a ``MockTransport`` here means zero real network traffic.
        self._transport = transport

    async def _load_credentials(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        channel_type: str,
        primary_credential_type: str,
    ) -> tuple[Mapping[str, Any], Any]:
        return await load_channel_credentials(
            self._vault,
            session,
            tenant_id,
            channel_account_id,
            channel_type,
            primary_credential_type,
        )

    async def publish_listing(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: MarketplaceListingPayload,
    ) -> MarketplaceListingDispatchReceipt:
        channel_type = payload.channel_type.strip().lower()
        credential_type = CREDENTIAL_TYPES.get(channel_type)
        if credential_type is None:
            raise unsupported_channel_error(channel_type)

        credentials, expires_at = await self._load_credentials(
            session,
            tenant_id,
            payload.channel_account_id,
            channel_type,
            credential_type,
        )
        # A per-marketplace ``category_attributes`` blob (``category_id`` for Shopee,
        # ``primary_category`` for Ozon, …) overrides the catalog category text for THIS
        # marketplace only. With no attributes the old ``payload.category`` is used verbatim.
        cat_attrs = dict(getattr(payload, "category_attributes", {}) or {})
        effective_category = (
            str(
                cat_attrs.get("category_id")
                or cat_attrs.get("primary_category")
                or cat_attrs.get("category")
                or payload.category
            ).strip()
            or payload.category
        )
        adapter = build_marketplace_adapter(
            channel_type,
            credentials,
            transport=self._transport,
            expires_at=expires_at,
            tenant_id=tenant_id,
            channel_account_id=payload.channel_account_id,
            vault=self._vault,
            session=session,
        )
        if adapter is None:  # pragma: no cover - guarded by CREDENTIAL_TYPES above
            raise unsupported_channel_error(channel_type)
        # The eight global marketplaces implement the catalog-side
        # ``publish_listing(payload)`` contract directly; the storefront connectors still take
        # the legacy keyword form. Both narrowings stay explicit so mypy can prove the call.
        if isinstance(
            adapter,
            (
                ShopeeAdapter,
                LazadaAdapter,
                TemuAdapter,
                SheinAdapter,
                AliExpressAdapter,
                OzonAdapter,
                WalmartAdapter,
                MercadoLibreAdapter,
            ),
        ):
            effective_payload = (
                payload
                if effective_category == payload.category
                else payload.model_copy(update={"category": effective_category})
            )
            payload_receipt = await adapter.publish_listing(effective_payload)
            dispatch_receipt = _normalize_payload_receipt(payload_receipt)
        else:
            # `build_marketplace_adapter` returns the shared connector ABC (it also feeds the
            # order reconciler), so narrow to the eight legacy storefront marketplaces.
            listing_adapter = cast(
                AmazonSpApiAdapter
                | EbayConnectorAdapter
                | AlibabaIntlAdapter
                | ShopifyGraphqlAdapter
                | WooCommerceAdapter
                | ShoplineAdapter
                | ShoplazzaAdapter
                | TikTokShopCoreAdapter,
                adapter,
            )
            # The legacy storefront connectors return their own receipt models; normalize
            # them onto the catalog DTO so the caller sees one shape for all 16 channels.
            legacy_receipt = await listing_adapter.publish_listing(
                sku=payload.external_sku,
                title=payload.title,
                description=payload.description,
                bullet_points=payload.bullet_points,
                price_usd=payload.price_usd,
                quantity=payload.quantity,
                images=payload.images,
                category=effective_category,
                hs_code=payload.hs_code,
                moq_tiers=[
                    {
                        "min_qty": tier.min_qty,
                        "unit_price_usd": tier.unit_price_usd,
                        "min_quantity": tier.min_qty,
                        "unit_price": tier.unit_price_usd,
                    }
                    for tier in payload.moq_tiers
                ],
                options=[opt.model_dump() for opt in payload.options],
                variants=[
                    {
                        "local_sku": v.local_sku,
                        "external_sku": v.external_sku,
                        "option_values": dict(v.option_values),
                        "price_usd": v.price_usd,
                        "quantity": v.quantity,
                        "image_url": v.image_url,
                    }
                    for v in payload.variants
                ],
                image_blobs=[
                    {
                        "filename": b.filename,
                        "mime_type": b.mime_type,
                        "data": b.data,
                        "alt_text": b.alt_text,
                    }
                    for b in payload.image_blobs
                ],
            )
            dispatch_receipt = MarketplaceListingDispatchReceipt(
                external_product_id=legacy_receipt.external_product_id,
                external_sku=legacy_receipt.external_sku,
                status=legacy_receipt.status,
                variant_receipts=_coerce_variant_receipts(
                    getattr(legacy_receipt, "variant_receipts", None),
                    fallback_sku=legacy_receipt.external_sku,
                ),
                raw_response=dict(legacy_receipt.raw_response),
            )
        logger.info(
            "marketplace_listing_dispatched",
            tenant_id=str(tenant_id),
            channel_account_id=str(payload.channel_account_id),
            channel_type=channel_type,
            external_sku=payload.external_sku,
            external_product_id=dispatch_receipt.external_product_id,
            status=dispatch_receipt.status,
        )
        return dispatch_receipt


__all__ = [
    "ALIBABA_INTL_CHANNEL_TYPE",
    "ALIBABA_INTL_CREDENTIAL_TYPE",
    "ALIEXPRESS_CHANNEL_TYPE",
    "ALIEXPRESS_CREDENTIAL_TYPE",
    "AMAZON_CREDENTIAL_TYPE",
    "AMAZON_SP_CHANNEL_TYPE",
    "CREDENTIAL_TYPES",
    "DEFAULT_AMAZON_MARKETPLACE_ID",
    "DEFAULT_EBAY_SITE_ID",
    "DEFAULT_FULFILLMENT_MODE",
    "DEFAULT_LAZADA_REGION",
    "DEFAULT_MERCADO_LIBRE_SITE_ID",
    "DEFAULT_OZON_FULFILLMENT_MODE",
    "DEFAULT_SHOPEE_REGION",
    "DEFAULT_SHEIN_REGION",
    "DEFAULT_TEMU_REGION",
    "EBAY_CHANNEL_TYPE",
    "EBAY_CREDENTIAL_TYPE",
    "LAZADA_CHANNEL_TYPE",
    "LAZADA_CREDENTIAL_TYPE",
    "MarketplaceListingDispatcher",
    "MERCADO_LIBRE_CHANNEL_TYPE",
    "MERCADO_LIBRE_CREDENTIAL_TYPE",
    "OZON_CHANNEL_TYPE",
    "OZON_CREDENTIAL_TYPE",
    "REQUIRED_CREDENTIAL_KEYS",
    "SHEIN_CHANNEL_TYPE",
    "SHEIN_CREDENTIAL_TYPE",
    "SHOPEE_CHANNEL_TYPE",
    "SHOPEE_CREDENTIAL_TYPE",
    "SHOPIFY_CHANNEL_TYPE",
    "SHOPIFY_CREDENTIAL_TYPE",
    "SHOPIFY_FALLBACK_CREDENTIAL_TYPE",
    "SHOPLAZZA_CHANNEL_TYPE",
    "SHOPLAZZA_CREDENTIAL_TYPE",
    "SHOPLINE_CHANNEL_TYPE",
    "SHOPLINE_CREDENTIAL_TYPE",
    "SUPPORTED_CHANNEL_TYPES",
    "TEMU_CHANNEL_TYPE",
    "TEMU_CREDENTIAL_TYPE",
    "TIKTOK_SHOP_CHANNEL_TYPE",
    "TIKTOK_SHOP_CREDENTIAL_TYPE",
    "WALMART_CHANNEL_TYPE",
    "WALMART_CREDENTIAL_TYPE",
    "WOOCOMMERCE_CHANNEL_TYPE",
    "WOOCOMMERCE_CREDENTIAL_TYPE",
    "build_marketplace_adapter",
    "load_channel_credentials",
    "unsupported_channel_error",
]
