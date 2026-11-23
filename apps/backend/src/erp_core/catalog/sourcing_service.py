"""Miaoshou-style product sourcing: collect supplier data, price it, import, publish.

Pipeline contract:

``collect``  -> SSRF-guarded public-page fetch (or pasted copy) -> Decimal pricing
             -> English SEO copy + canonical SKU -> ``sourcing_drafts`` row.
``import``   -> real ``products`` + ``product_variants`` rows.
``publish``  -> ``platform_sku_mappings`` per target channel account + history.

The module intentionally imports only ``erp_core.catalog``, ``erp_core.shared`` and
``erp_core.tenancy`` so ``infra/importlinter.ini`` ``catalog_isolated`` stays green
(catalog must never reach into orders/finance/ai_engine/channels).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import unescape
from typing import Any, Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import structlog
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.media_service import CatalogMediaService
from erp_core.catalog.schemas import MoqTierInput, ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

_CENT = Decimal("0.01")
_COST_SCALE = Decimal("0.0001")
_HUNDRED = Decimal("100")
_MAX_TARGET_MARGIN_PCT = Decimal("90.00")
_MAX_FETCH_CHARS = 512_000
# Hard cap on the raw bytes we are willing to pull from a third-party page.
MAX_SOURCE_FETCH_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 3.0
_DEFAULT_WEIGHT_GRAMS = 500
_DEFAULT_LEAD_TIME_DAYS = 7
_DEFAULT_MIN_MARGIN_FLOOR_PCT = Decimal("15.00")
_USER_AGENT = "OmniERP-SourcingBot/1.0 (+https://erp.local/sourcing)"

# Honest listing bookkeeping. ``sourcing_drafts.status`` is pinned by the 0012 DB CHECK
# constraint to ``('draft', 'imported', 'published')``; publishing only writes a local
# ``platform_sku_mappings`` row and never calls a marketplace API, so a remote product
# id is intentionally explicit about being pending rather than invented.
PENDING_LISTING_PREFIX = "pending-listing:"
ListingState = Literal["draft", "imported", "mapping_ready", "live_published"]

# (min_qty, volume discount applied to the tier-1 target retail price)
_MOQ_LADDER: tuple[tuple[int, Decimal], ...] = (
    (100, Decimal("0.00")),
    (500, Decimal("0.05")),
    (2000, Decimal("0.10")),
)

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "kubernetes.default",
    }
)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet")
_BLOCKED_URL_SCHEMES = frozenset({"file", "ftp", "gopher", "data", "dict", "ldap", "javascript"})
# RFC 6598 carrier-grade NAT space: not ``is_private`` on every CPython release.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

DnsResolver = Callable[[str], Awaitable[Sequence[str]]]

_CNY_PREFIX_PRICE_RE = re.compile(
    r"(?:¥|￥|RMB|CNY)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE
)
_CNY_SUFFIX_PRICE_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块)")
_IMAGE_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp|gif)", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r"(?P<key>[A-Za-z:_-]+)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s\"'>]+))",
    re.IGNORECASE,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+\-]*")
_SKU_PLATFORM_RE = re.compile(r"[^A-Za-z0-9]")

_CAPTCHA_MARKERS: tuple[str, ...] = (
    "captcha",
    "security check",
    "滑动验证",
    "验证码",
    "punish",
    "unusual traffic",
)
_NOISE_FRAGMENTS: tuple[str, ...] = (
    "阿里巴巴",
    "1688.com",
    "alibaba.com",
    "alibaba group",
    "厂家直销",
    "一件代发",
    "淘宝",
    "拼多多",
    "京东",
)

# Deterministic CN -> EN glossary for the export categories this ERP ships most.
_EN_GLOSSARY: dict[str, str] = {
    "太阳能": "Solar",
    "投光灯": "Flood Light",
    "洗墙灯": "Wall Washer",
    "庭院灯": "Garden Light",
    "吸顶灯": "Ceiling Light",
    "吊灯": "Pendant Light",
    "射灯": "Spotlight",
    "筒灯": "Downlight",
    "灯带": "Light Strip",
    "灯串": "String Light",
    "灯泡": "Bulb",
    "路灯": "Street Light",
    "工矿灯": "High Bay Light",
    "应急灯": "Emergency Light",
    "充电": "Rechargeable",
    "遥控": "Remote Control",
    "户外": "Outdoor",
    "室内": "Indoor",
    "防水": "Waterproof",
    "可调光": "Dimmable",
    "智能": "Smart",
    "人体感应": "Motion Sensor",
    "铝": "Aluminum",
    "不锈钢": "Stainless Steel",
    "灯": "Light",
    "套": "Set",
    "件": "Pack",
    "个": "Pc",
    "瓦": "W",
    "米": "Meter",
}


def _sourcing_error(slug: str, code: str, detail: str, status_code: int = 422) -> AppError:
    return AppError(
        slug=slug,
        status_code=status_code,
        code=code,
        title="Sourcing Request Rejected",
        detail=detail,
    )


def _decimal_param(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise _sourcing_error(
            "invalid-sourcing-decimal", "ERR_INVALID_SOURCING_DECIMAL", f"{name} must be a Decimal."
        )
    if not value.is_finite():
        raise _sourcing_error(
            "invalid-sourcing-decimal",
            "ERR_INVALID_SOURCING_DECIMAL",
            f"{name} must be a finite Decimal.",
        )
    return value


def _as_json_list(value: Any) -> list[Any]:
    """JSONB round-trips as a list, but tolerate a raw JSON string from any driver."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return list(value)
    return []


def _as_json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def draft_listing_state(
    status: str, publish_history: Sequence[Mapping[str, Any]]
) -> ListingState:
    """Derive the honest listing state from the persisted status + publish history.

    ``published`` only proves a local SKU mapping was stored, so it maps to
    ``mapping_ready`` unless a history entry explicitly confirms an external listing.
    """
    if status == "published":
        for entry in publish_history:
            if entry.get("external_listing_confirmed") is True:
                return "live_published"
        return "mapping_ready"
    if status == "imported":
        return "imported"
    return "draft"


def pending_listing_product_id(external_sku: str) -> str:
    """Placeholder remote product id that never impersonates a live marketplace id."""
    return f"{PENDING_LISTING_PREFIX}{external_sku}"


def _to_decimal(raw: str) -> Decimal | None:
    cleaned = raw.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


# --------------------------------------------------------------------------------------
# SSRF guard
# --------------------------------------------------------------------------------------


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Fail closed: only globally routable unicast addresses are acceptable."""
    mapped = getattr(value, "ipv4_mapped", None)
    if mapped is not None:
        # ``::ffff:127.0.0.1`` is a loopback target wearing an IPv6 costume.
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


def validate_source_url(raw: str) -> str:
    """Allow only plain http(s) URLs pointing at a public internet host.

    Blocks loopback, RFC1918, link-local (169.254.0.0/16 metadata), CGNAT,
    unique-local IPv6, single-label intranet names and credential-smuggling URLs.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise _sourcing_error(
            "unsafe-source-url", "ERR_UNSAFE_SOURCE_URL", "Source URL must not be empty."
        )
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme in _BLOCKED_URL_SCHEMES or scheme not in {"http", "https"}:
        raise _sourcing_error(
            "unsafe-source-url",
            "ERR_UNSAFE_SOURCE_URL",
            f"Scheme '{parsed.scheme}' is not allowed; only http/https are supported.",
        )
    if parsed.username or parsed.password:
        raise _sourcing_error(
            "unsafe-source-url",
            "ERR_UNSAFE_SOURCE_URL",
            "Source URL must not embed credentials.",
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise _sourcing_error(
            "unsafe-source-url", "ERR_UNSAFE_SOURCE_URL", "Source URL must include a hostname."
        )
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise _sourcing_error(
            "unsafe-source-url",
            "ERR_UNSAFE_SOURCE_URL",
            f"Host '{host}' is a private or reserved target.",
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise _sourcing_error(
                "unsafe-source-url",
                "ERR_UNSAFE_SOURCE_URL",
                f"IP literal '{host}' is not a public internet address.",
            )
    elif "." not in host:
        raise _sourcing_error(
            "unsafe-source-url",
            "ERR_UNSAFE_SOURCE_URL",
            f"Host '{host}' is not a fully qualified public domain.",
        )
    return candidate


def _blocked_source_error(host: str, detail: str) -> AppError:
    return _sourcing_error("blocked-source-url", "ERR_SOURCING_URL_BLOCKED", f"{host}: {detail}")


async def system_dns_resolver(host: str) -> Sequence[str]:
    """Resolve ``host`` through the platform stub resolver (``()`` when unknown)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return ()
    return tuple({str(info[4][0]) for info in infos})


async def resolve_and_validate_public_url(
    url: str,
    *,
    dns_resolver: DnsResolver | None = None,
) -> tuple[str, list[str]]:
    """Validate a source URL *and* every address its hostname currently resolves to.

    ``validate_source_url`` only inspects the URL text, so a hostile hostname such as
    ``127.0.0.1.nip.io`` slips through it. Resolving here — immediately before the
    request — defeats DNS-rebinding style tricks: any answer that is not globally
    routable rejects the whole URL.
    """
    safe_url = validate_source_url(url)
    host = (urlparse(safe_url).hostname or "").strip().lower().rstrip(".")
    resolver = dns_resolver or system_dns_resolver
    try:
        answers = await resolver(host)
    except OSError as exc:
        raise _blocked_source_error(
            host, f"DNS resolution failed ({type(exc).__name__})"
        ) from exc
    addresses = sorted(
        {str(answer).strip().strip("[]") for answer in answers if str(answer).strip()}
    )
    if not addresses:
        raise _blocked_source_error(host, "hostname did not resolve to any address")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise _blocked_source_error(
                host, f"hostname resolved to an unparsable address '{address}'"
            ) from exc
        if not _is_public_ip(resolved):
            raise _blocked_source_error(
                host, f"hostname resolves to non-public address '{address}'"
            )
    return safe_url, addresses


# --------------------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------------------


class SourcingMoqTier(BaseModel):
    min_qty: int = Field(gt=0)
    unit_price_usd: Decimal = Field(gt=Decimal("0"))


class SourcingOptionSpec(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    values: list[str] = Field(min_length=1, max_length=50)


class SourcingVariantSpec(BaseModel):
    sku_suffix: str = Field(min_length=1, max_length=40)
    option_values: dict[str, str] = Field(default_factory=dict)
    cost_price_cny: Decimal = Field(gt=Decimal("0"))
    retail_price_usd: Decimal | None = Field(default=None, gt=Decimal("0"))
    image_url: str | None = Field(default=None, max_length=2048)
    initial_stock: int = Field(default=100, ge=0, le=1_000_000)


class MarketplaceImageBlob(BaseModel):
    filename: str
    mime_type: str
    data: bytes
    alt_text: str = ""


class MarketplaceVariantPayload(BaseModel):
    local_variant_id: UUID | None = None
    local_sku: str
    external_sku: str
    option_values: dict[str, str] = Field(default_factory=dict)
    price_usd: Decimal
    quantity: int = 0
    image_url: str | None = None


class MarketplaceVariantReceipt(BaseModel):
    external_sku: str
    external_variant_id: str | None = None
    external_inventory_item_id: str | None = None


class MarketplaceListingPayload(BaseModel):
    """Everything a marketplace API needs to create one live listing.

    Pure catalog-side DTO: it carries no channel/order/finance types, so
    ``catalog_isolated`` stays green and the concrete adapter arrives through
    ``MarketplaceListingPublisher`` injection (``app.state`` in ``api_main``).
    """

    channel_account_id: UUID
    channel_type: str
    external_sku: str
    title: str
    description: str
    bullet_points: list[str]
    price_usd: Decimal
    quantity: int
    images: list[str]
    category: str
    hs_code: str | None = None
    moq_tiers: list[SourcingMoqTier] = Field(default_factory=list)
    options: list[SourcingOptionSpec] = Field(default_factory=list)
    variants: list[MarketplaceVariantPayload] = Field(default_factory=list)
    image_blobs: list[MarketplaceImageBlob] = Field(default_factory=list)


class MarketplaceListingDispatchReceipt(BaseModel):
    """Proof a marketplace accepted a listing, plus the id the ERP must persist."""

    external_product_id: str
    external_sku: str
    status: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    variant_receipts: list[MarketplaceVariantReceipt] = Field(default_factory=list)


class MarketplaceListingPublisher(Protocol):
    """Structural contract for the live marketplace push (implemented in ``channels``)."""

    async def publish_listing(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: MarketplaceListingPayload,
    ) -> MarketplaceListingDispatchReceipt: ...


class SourcingCollectRequest(BaseModel):
    source_platform: Literal["1688", "url", "manual"] = "1688"
    source_url: str | None = Field(default=None, max_length=2048)
    raw_title: str | None = Field(default=None, max_length=512)
    raw_text: str | None = Field(default=None, max_length=20000)
    raw_price_cny: Decimal | None = Field(default=None, ge=Decimal("0"))
    images: list[str] = Field(default_factory=list, max_length=20)
    target_margin_pct: Decimal = Field(
        default=Decimal("35.00"), ge=Decimal("0"), lt=_MAX_TARGET_MARGIN_PCT
    )
    fx_rate_usd_cny: Decimal = Field(default=Decimal("7.200000"), gt=Decimal("0"))
    category: str = Field(default="LED Lighting", min_length=1, max_length=128)
    hs_code: str | None = Field(default=None, max_length=32)
    options_spec: list[SourcingOptionSpec] = Field(default_factory=list, max_length=5)
    variants_spec: list[SourcingVariantSpec] = Field(default_factory=list, max_length=50)


class SourcingPageMetadata(BaseModel):
    title: str | None = None
    og_title: str | None = None
    og_image: str | None = None
    og_description: str | None = None
    price_cny: Decimal | None = None

    @property
    def best_title(self) -> str | None:
        return self.og_title or self.title

    def is_empty(self) -> bool:
        return not any(
            (self.title, self.og_title, self.og_image, self.og_description, self.price_cny)
        )


class SourcingSource(BaseModel):
    title: str | None = None
    description: str | None = None
    price_cny: Decimal | None = None
    images: list[str] = Field(default_factory=list)


class SourcingPricing(BaseModel):
    cost_usd: Decimal
    target_price_usd: Decimal
    moq_tiers: list[SourcingMoqTier]


class PlannedSourcingDraft(BaseModel):
    """Everything needed to insert one ``sourcing_drafts`` row."""

    source_platform: str
    source_url: str | None
    raw_title: str
    raw_price_cny: Decimal
    fx_rate_usd_cny: Decimal
    target_margin_pct: Decimal
    cost_usd: Decimal
    optimized_title_en: str
    bullet_points: list[str]
    suggested_sku: str
    suggested_category: str
    suggested_hs_code: str | None
    target_price_usd: Decimal
    moq_tiers: list[SourcingMoqTier]
    images: list[str]
    attributes: dict[str, Any]
    options_spec: list[SourcingOptionSpec] = Field(default_factory=list)
    variants_spec: list[SourcingVariantSpec] = Field(default_factory=list)


class SourcingDraftRead(BaseModel):
    id: UUID
    tenant_id: UUID
    source_platform: str
    source_url: str | None
    raw_title: str
    raw_price_cny: Decimal
    fx_rate_usd_cny: Decimal
    target_margin_pct: Decimal
    cost_usd: Decimal
    optimized_title_en: str
    bullet_points: list[str]
    suggested_sku: str
    suggested_category: str
    suggested_hs_code: str | None
    target_price_usd: Decimal
    moq_tiers: list[SourcingMoqTier]
    images: list[str]
    attributes: dict[str, Any]
    options_spec: list[SourcingOptionSpec] = Field(default_factory=list)
    variants_spec: list[SourcingVariantSpec] = Field(default_factory=list)
    status: str
    imported_product_id: UUID | None = None
    imported_variant_id: UUID | None = None
    publish_history: list[dict[str, Any]] = Field(default_factory=list)
    # Honest double bookkeeping: ``status`` stays bound to the 0012 DB CHECK
    # (``draft``/``imported``/``published``), while ``listing_state`` records what
    # publishing actually achieved. ``published`` only means "a local SKU mapping
    # exists"; ``live_published`` requires an explicitly confirmed external listing.
    listing_state: Literal["draft", "imported", "mapping_ready", "live_published"] = "draft"
    created_at: datetime
    updated_at: datetime


class SourcingImportResult(BaseModel):
    draft: SourcingDraftRead
    product_id: UUID
    variant_id: UUID
    sku: str
    retail_price_usd: Decimal
    created: bool


class SourcingPublishRequest(BaseModel):
    channel_account_ids: list[UUID] = Field(min_length=1, max_length=50)
    price_multiplier: Decimal = Field(default=Decimal("1.00"), gt=Decimal("0"), le=Decimal("10"))
    stock_sync_ratio: Decimal = Field(default=Decimal("1.00"), ge=Decimal("0"), le=Decimal("1"))
    #: Opt-in live push. Default ``False`` keeps the historic local-SKU-mapping behavior
    #: (and therefore every existing caller/test) byte-identical.
    push_to_marketplace: bool = False
    #: Sellable quantity pushed with a live listing; ignored by the local mapping path.
    initial_stock_quantity: int = Field(default=100, ge=1, le=1_000_000)


class SourcingPublishResult(BaseModel):
    channel_account_id: UUID
    channel_type: str
    external_sku: str
    external_product_id: str
    price_usd: Decimal
    stock_sync_ratio: Decimal
    mapping_id: UUID
    published_at: datetime
    # Publishing only persists a local SKU mapping: no marketplace API is called, so
    # nothing may be reported as a live listing until it is confirmed out of band.
    listing_state: Literal["mapping_ready", "live_published"] = "mapping_ready"
    external_listing_confirmed: bool = False
    publish_mode: Literal["local_sku_mapping", "live_marketplace_api"] = "local_sku_mapping"
    remote_status: str | None = Field(default=None, exclude=True)


class SourcingDeleteResult(BaseModel):
    deleted: bool
    draft_id: UUID


# --------------------------------------------------------------------------------------
# Pure parsing / pricing / copywriting helpers
# --------------------------------------------------------------------------------------


def _first_cny_price(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    for pattern in (_CNY_PREFIX_PRICE_RE, _CNY_SUFFIX_PRICE_RE):
        match = pattern.search(raw)
        if match:
            value = _to_decimal(match.group(1))
            if value is not None and value > 0:
                return value
    return None


def _clean_text(raw: str) -> str:
    return _WHITESPACE_RE.sub(" ", unescape(_TAG_STRIP_RE.sub(" ", raw))).strip()


def _strip_noise(raw: str) -> str:
    cleaned = raw
    for fragment in _NOISE_FRAGMENTS:
        cleaned = re.sub(re.escape(fragment), " ", cleaned, flags=re.IGNORECASE)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    # Drop wrapper punctuation left behind by the removed promo fragments.
    cleaned = re.sub(r"[\[【(（]\s*[\]】)）]", " ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip(" -|·\t【】[]()（）")


def _dedupe(values: Sequence[str], limit: int = 5) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        token = value.strip()
        key = token.lower()
        if not token or key in seen:
            continue
        seen.add(key)
        result.append(token)
        if len(result) >= limit:
            break
    return result


def parse_supplier_copy(raw_title: str | None, raw_text: str | None) -> SourcingSource:
    """Parse 1688/supplier pasted copy into a normalized source (CNY price, images)."""
    text_blob = (raw_text or "").strip()
    title = (raw_title or "").strip()
    if not title and text_blob:
        title = next((line.strip() for line in text_blob.splitlines() if line.strip()), "")
    title = _strip_noise(title)[:200]
    price = _first_cny_price(text_blob) or _first_cny_price(title)
    images = _dedupe(_IMAGE_URL_RE.findall(text_blob))
    description = _WHITESPACE_RE.sub(" ", text_blob)[:500] or None
    return SourcingSource(
        title=title or None,
        description=description,
        price_cny=price,
        images=images,
    )


def _meta_attributes(tag: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in _META_ATTR_RE.finditer(tag):
        key = match.group("key").lower()
        value = match.group("dq") or match.group("sq") or match.group("bare") or ""
        attributes.setdefault(key, unescape(value))
    return attributes


def parse_page_metadata(html: str) -> SourcingPageMetadata:
    """Extract ``<title>``, OpenGraph tags and a plausible CNY price from public HTML."""
    if not html or not html.strip():
        return SourcingPageMetadata()
    title_match = _TITLE_TAG_RE.search(html)
    title = _clean_text(title_match.group("title")) if title_match else None
    og_title: str | None = None
    og_image: str | None = None
    og_description: str | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    for tag in _META_TAG_RE.findall(html):
        attributes = _meta_attributes(tag)
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        content = attributes.get("content", "").strip()
        if not key or not content:
            continue
        if key in {"og:title", "twitter:title"} and og_title is None:
            og_title = _clean_text(content) or None
        elif key in {"og:image", "twitter:image"} and og_image is None:
            og_image = content
        elif key in {"og:description", "twitter:description", "description"}:
            og_description = og_description or _clean_text(content) or None
        elif key in {"og:price:amount", "product:price:amount"}:
            price_amount = _to_decimal(content)
        elif key in {"og:price:currency", "product:price:currency"}:
            price_currency = content.upper()
    price: Decimal | None = None
    if price_amount is not None and price_amount > 0 and price_currency in (None, "", "CNY", "RMB"):
        price = price_amount
    if price is None:
        price = _first_cny_price(_clean_text(html))
    return SourcingPageMetadata(
        title=title,
        og_title=og_title,
        og_image=og_image,
        og_description=og_description,
        price_cny=price,
    )


def compute_sourcing_pricing(
    raw_price_cny: Decimal,
    fx_rate_usd_cny: Decimal,
    target_margin_pct: Decimal,
) -> SourcingPricing:
    """Decimal-only CNY->USD cost and margin-based retail with a 3-tier MOQ ladder."""
    price = _decimal_param("raw_price_cny", raw_price_cny)
    fx_rate = _decimal_param("fx_rate_usd_cny", fx_rate_usd_cny)
    margin = _decimal_param("target_margin_pct", target_margin_pct)
    if price < 0:
        raise _sourcing_error(
            "invalid-sourcing-price", "ERR_INVALID_SOURCING_PRICE", "raw_price_cny must be >= 0."
        )
    if fx_rate <= 0:
        raise _sourcing_error(
            "invalid-sourcing-fx", "ERR_INVALID_SOURCING_FX", "fx_rate_usd_cny must be > 0."
        )
    if margin < 0 or margin >= _MAX_TARGET_MARGIN_PCT:
        raise _sourcing_error(
            "invalid-sourcing-margin",
            "ERR_INVALID_SOURCING_MARGIN",
            "target_margin_pct must be >= 0 and < 90.",
        )
    cost_usd = (price / fx_rate).quantize(_COST_SCALE, rounding=ROUND_HALF_UP)
    divisor = Decimal(1) - (margin / _HUNDRED)
    target_price_usd = (cost_usd / divisor).quantize(_CENT, rounding=ROUND_HALF_UP)
    tiers = [
        SourcingMoqTier(
            min_qty=min_qty,
            unit_price_usd=(target_price_usd * (Decimal(1) - discount)).quantize(
                _CENT, rounding=ROUND_HALF_UP
            ),
        )
        for min_qty, discount in _MOQ_LADDER
    ]
    return SourcingPricing(
        cost_usd=cost_usd, target_price_usd=target_price_usd, moq_tiers=tiers
    )


def _english_keywords(raw_title: str) -> list[str]:
    events: list[tuple[int, str]] = []
    glossary_keys = sorted(_EN_GLOSSARY, key=len, reverse=True)
    index = 0
    while index < len(raw_title):
        matched_key = next(
            (key for key in glossary_keys if raw_title.startswith(key, index)), None
        )
        if matched_key is None:
            index += 1
            continue
        events.append((index, _EN_GLOSSARY[matched_key]))
        index += len(matched_key)
    for match in _ASCII_TOKEN_RE.finditer(raw_title):
        token = match.group(0)
        if token.lower() in {"the", "and", "for", "with", "wholesale"}:
            continue
        events.append((match.start(), token))
    events.sort(key=lambda item: item[0])
    seen: set[str] = set()
    keywords: list[str] = []
    for _, token in events:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(token)
    return keywords


def build_english_title(raw_title: str, category: str) -> str:
    """Deterministic export-ready English title (keeps acronyms/units, translates keywords)."""
    keywords = _english_keywords(raw_title)[:14]
    if not keywords:
        keywords = _english_keywords(category)[:6]
        if not keywords:
            return f"{category} Wholesale Supplier Direct"[:200]
        keywords.append("Wholesale")
    title = " ".join(keywords).strip()
    if len(title) < 10:
        title = f"{category} Wholesale Supplier Direct"
    return title[:200]


def build_selling_points(
    optimized_title_en: str,
    *,
    category: str,
    pricing: SourcingPricing,
    hs_code: str | None,
) -> list[str]:
    """Exactly five export bullets for Amazon/eBay/Alibaba listings."""
    ladder = " / ".join(f"{tier.min_qty:,}" for tier in pricing.moq_tiers)
    headline = optimized_title_en[:90]
    points = [
        f"Factory-direct {category}: {headline}",
        "Export-ready build with CE/RoHS documentation and QC inspection before shipment",
        f"B2B MOQ ladder {ladder} units with volume pricing down to "
        f"${pricing.moq_tiers[-1].unit_price_usd}",
        f"Customs-ready under {hs_code or 'HS-classified'} tariff code "
        "with DDP, air and sea options",
        "OEM/ODM private label, custom packaging, manual localisation and 12-month warranty",
    ]
    return points[:5]


def build_suggested_sku(
    source_platform: str,
    source_url: str | None,
    raw_title: str,
    raw_price_cny: Decimal,
) -> str:
    """Canonical sourcing SKU, e.g. ``SRC-1688-9F3A`` (stable for identical inputs)."""
    platform_token = _SKU_PLATFORM_RE.sub("", source_platform.upper()) or "SRC"
    seed = "|".join([platform_token, source_url or "", raw_title, str(raw_price_cny)])
    digest = hashlib.blake2s(seed.encode("utf-8"), digest_size=8).hexdigest().upper()[:4]
    return f"SRC-{platform_token}-{digest}"


def channel_sku_prefix(channel_type: str) -> str:
    """``amazon_sp`` -> ``AMAZON``, ``ebay`` -> ``EBAY``, ``alibaba_email`` -> ``ALIBABA``."""
    primary = channel_type.split("_", 1)[0]
    token = _SKU_PLATFORM_RE.sub("", primary.upper())
    return token or "CHANNEL"


def build_external_sku(channel_type: str, sku: str) -> str:
    return f"{channel_sku_prefix(channel_type)}-{sku}"


def build_listing_description(draft: SourcingDraftRead) -> str:
    """Deterministic English listing body pushed to a live marketplace.

    Prefers the supplier description captured at collection time and degrades to the
    optimized title, so a live listing never ships an empty description.
    """
    source = _WHITESPACE_RE.sub(
        " ", str(draft.attributes.get("source_description") or "")
    ).strip()
    return source[:2000] or draft.optimized_title_en


def plan_sourcing_draft(
    payload: SourcingCollectRequest, source: SourcingSource
) -> PlannedSourcingDraft:
    """Merge request + parsed source into the exact draft row to persist."""
    title = (source.title or payload.raw_title or "").strip()
    if not title:
        raise _sourcing_error(
            "sourcing-title-required",
            "ERR_SOURCING_TITLE_REQUIRED",
            "A product title is required: provide raw_title/raw_text or a fetchable source_url.",
        )
    raw_price = payload.raw_price_cny if payload.raw_price_cny is not None else source.price_cny
    if raw_price is None and payload.variants_spec:
        raw_price = min(v.cost_price_cny for v in payload.variants_spec)
    if raw_price is None:
        raise _sourcing_error(
            "sourcing-price-required",
            "ERR_SOURCING_PRICE_REQUIRED",
            "A CNY source price is required: provide raw_price_cny or include it in raw_text.",
        )
    pricing = compute_sourcing_pricing(
        raw_price, payload.fx_rate_usd_cny, payload.target_margin_pct
    )
    images = _dedupe([*payload.images, *source.images], limit=10)
    optimized_title_en = build_english_title(title, payload.category)
    hs_code = (payload.hs_code or "").strip() or None
    normalized_variants: list[SourcingVariantSpec] = []
    seen_suffixes: set[str] = set()
    for idx, v in enumerate(payload.variants_spec, start=1):
        suffix = _SKU_PLATFORM_RE.sub("-", v.sku_suffix.strip().upper()).strip("-") or f"V{idx}"
        if suffix in seen_suffixes:
            suffix = f"{suffix}-{idx}"
        seen_suffixes.add(suffix)
        computed_usd = (
            v.retail_price_usd
            if v.retail_price_usd is not None
            else compute_sourcing_pricing(
                v.cost_price_cny, payload.fx_rate_usd_cny, payload.target_margin_pct
            ).target_price_usd
        )
        normalized_variants.append(
            SourcingVariantSpec(
                sku_suffix=suffix,
                option_values={str(k): str(val) for k, val in v.option_values.items()},
                cost_price_cny=v.cost_price_cny,
                retail_price_usd=computed_usd,
                image_url=v.image_url,
                initial_stock=v.initial_stock,
            )
        )
    options_spec = list(payload.options_spec)
    if normalized_variants and not options_spec:
        option_map: dict[str, list[str]] = {}
        for variant in normalized_variants:
            for opt_name, opt_val in variant.option_values.items():
                values = option_map.setdefault(opt_name, [])
                if opt_val not in values:
                    values.append(opt_val)
        options_spec = [
            SourcingOptionSpec(name=name, values=vals)
            for name, vals in option_map.items()
            if vals
        ]
    return PlannedSourcingDraft(
        source_platform=payload.source_platform,
        source_url=payload.source_url,
        raw_title=title[:512],
        raw_price_cny=raw_price,
        fx_rate_usd_cny=payload.fx_rate_usd_cny,
        target_margin_pct=payload.target_margin_pct,
        cost_usd=pricing.cost_usd,
        optimized_title_en=optimized_title_en,
        bullet_points=build_selling_points(
            optimized_title_en,
            category=payload.category,
            pricing=pricing,
            hs_code=hs_code,
        ),
        suggested_sku=build_suggested_sku(
            payload.source_platform, payload.source_url, title, raw_price
        ),
        suggested_category=payload.category.strip(),
        suggested_hs_code=hs_code,
        target_price_usd=pricing.target_price_usd,
        moq_tiers=pricing.moq_tiers,
        images=images,
        attributes={
            "sourcing_platform": payload.source_platform,
            "source_title": title[:512],
            "source_description": (source.description or "")[:500],
            "cost_usd": str(pricing.cost_usd),
        },
        options_spec=options_spec,
        variants_spec=normalized_variants,
    )


def _weight_grams(attributes: Mapping[str, Any]) -> int:
    raw = attributes.get("weight_grams")
    if isinstance(raw, bool):
        return _DEFAULT_WEIGHT_GRAMS
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
        return int(raw.strip())
    return _DEFAULT_WEIGHT_GRAMS


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )


async def _read_capped_body(response: httpx.Response) -> str:
    """Stream a response body, stopping hard at ``MAX_SOURCE_FETCH_BYTES`` bytes."""
    encoding = response.charset_encoding or "utf-8"
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        remaining = MAX_SOURCE_FETCH_BYTES - total
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            break
        chunks.append(chunk)
        total += len(chunk)
    try:
        body = b"".join(chunks).decode(encoding, errors="replace")
    except LookupError:  # an unknown charset label must not abort collection
        body = b"".join(chunks).decode("utf-8", errors="replace")
    return body[:_MAX_FETCH_CHARS]


class SourcingService:
    """Sourcing draft lifecycle: collect -> price -> import -> publish."""

    def __init__(
        self,
        catalog_service: CatalogService | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        dns_resolver: DnsResolver | None = None,
    ) -> None:
        self.catalog = catalog_service or CatalogService()
        self._client_factory = client_factory or _default_client_factory
        self._dns_resolver = dns_resolver

    # -- collection ------------------------------------------------------------------

    async def fetch_source_metadata(
        self, source_url: str, *, dns_resolver: DnsResolver | None = None
    ) -> SourcingPageMetadata:
        """Fetch a public page; never bypass captcha/anti-bot walls (graceful degrade)."""
        safe_url, _addresses = await resolve_and_validate_public_url(
            source_url, dns_resolver=dns_resolver or self._dns_resolver
        )
        try:
            async with (
                self._client_factory() as client,
                client.stream("GET", safe_url) as response,
            ):
                status_code = response.status_code
                body = await _read_capped_body(response) if status_code == 200 else ""
        except httpx.HTTPError as exc:
            logger.warning("sourcing.fetch_failed", url=safe_url, error=str(exc))
            return SourcingPageMetadata()
        lowered = body.lower()
        if status_code != 200 or any(marker in lowered for marker in _CAPTCHA_MARKERS):
            logger.warning("sourcing.fetch_blocked", url=safe_url, status=status_code)
            return SourcingPageMetadata()
        return parse_page_metadata(body)

    async def resolve_source(self, payload: SourcingCollectRequest) -> SourcingSource:
        pasted = parse_supplier_copy(payload.raw_title, payload.raw_text)
        if not payload.source_url:
            return pasted
        metadata = await self.fetch_source_metadata(payload.source_url)
        images = _dedupe([*pasted.images, *([metadata.og_image] if metadata.og_image else [])])
        return SourcingSource(
            title=metadata.best_title or pasted.title,
            description=metadata.og_description or pasted.description,
            price_cny=pasted.price_cny or metadata.price_cny,
            images=images,
        )

    async def collect(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: SourcingCollectRequest,
    ) -> SourcingDraftRead:
        source = await self.resolve_source(payload)
        planned = plan_sourcing_draft(payload, source)
        draft_id = uuid4()
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO sourcing_drafts (
                        id, tenant_id, source_platform, source_url, raw_title, raw_price_cny,
                        fx_rate_usd_cny, target_margin_pct, optimized_title_en, bullet_points,
                        suggested_sku, suggested_category, suggested_hs_code, target_price_usd,
                        moq_tiers, images, attributes, options_spec, variants_spec,
                        status, created_by_user_id
                    ) VALUES (
                        :id, :tid, :platform, :src_url, :raw_title, :raw_price,
                        :fx, :margin, :title_en, CAST(:bullets AS jsonb),
                        :sku, :category, :hs, :target_price,
                        CAST(:tiers AS jsonb), CAST(:images AS jsonb),
                        CAST(:attrs AS jsonb), CAST(:opts AS jsonb), CAST(:vars AS jsonb),
                        'draft', :actor
                    )
                    RETURNING *
                    """
                ),
                {
                    "id": draft_id,
                    "tid": tenant_id,
                    "platform": planned.source_platform,
                    "src_url": planned.source_url,
                    "raw_title": planned.raw_title,
                    "raw_price": planned.raw_price_cny,
                    "fx": planned.fx_rate_usd_cny,
                    "margin": planned.target_margin_pct,
                    "title_en": planned.optimized_title_en,
                    "bullets": json.dumps(planned.bullet_points, default=str),
                    "sku": planned.suggested_sku,
                    "category": planned.suggested_category,
                    "hs": planned.suggested_hs_code,
                    "target_price": planned.target_price_usd,
                    "tiers": json.dumps(
                        [
                            {"min_qty": t.min_qty, "unit_price_usd": str(t.unit_price_usd)}
                            for t in planned.moq_tiers
                        ]
                    ),
                    "images": json.dumps(planned.images, default=str),
                    "attrs": json.dumps(planned.attributes, default=str),
                    "opts": json.dumps(
                        [opt.model_dump() for opt in planned.options_spec], default=str
                    ),
                    "vars": json.dumps(
                        [
                            {
                                **v.model_dump(mode="json"),
                                "cost_price_cny": str(v.cost_price_cny),
                                "retail_price_usd": (
                                    str(v.retail_price_usd)
                                    if v.retail_price_usd is not None
                                    else None
                                ),
                            }
                            for v in planned.variants_spec
                        ],
                        default=str,
                    ),
                    "actor": actor_id,
                },
            )
        ).one()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sourcing_draft.collected",
            resource_type="sourcing_draft",
            resource_id=draft_id,
            after_state={
                "suggested_sku": planned.suggested_sku,
                "source_platform": planned.source_platform,
                "target_price_usd": str(planned.target_price_usd),
            },
        )
        return self.draft_from_mapping(row._mapping)

    # -- reads -----------------------------------------------------------------------

    @staticmethod
    def draft_from_mapping(data: Mapping[Any, Any]) -> SourcingDraftRead:
        tiers = [
            SourcingMoqTier(
                min_qty=int(tier["min_qty"]),
                unit_price_usd=Decimal(str(tier["unit_price_usd"])),
            )
            for tier in _as_json_list(data.get("moq_tiers"))
            if isinstance(tier, Mapping)
        ]
        images = [str(item) for item in _as_json_list(data.get("images"))]
        bullets = [str(item) for item in _as_json_list(data.get("bullet_points"))]
        history = [
            {str(key): value for key, value in entry.items()}
            for entry in _as_json_list(data.get("publish_history"))
            if isinstance(entry, Mapping)
        ]
        options_spec = [
            SourcingOptionSpec.model_validate(item)
            for item in _as_json_list(data.get("options_spec"))
            if isinstance(item, Mapping)
        ]
        variants_spec = [
            SourcingVariantSpec.model_validate(item)
            for item in _as_json_list(data.get("variants_spec"))
            if isinstance(item, Mapping)
        ]
        status = str(data["status"])
        raw_price = Decimal(str(data["raw_price_cny"]))
        fx_rate = Decimal(str(data["fx_rate_usd_cny"]))
        cost_usd = (raw_price / fx_rate).quantize(_COST_SCALE, rounding=ROUND_HALF_UP)
        imported_product_id = data.get("imported_product_id")
        imported_variant_id = data.get("imported_variant_id")
        return SourcingDraftRead(
            id=UUID(str(data["id"])),
            tenant_id=UUID(str(data["tenant_id"])),
            source_platform=str(data["source_platform"]),
            source_url=str(data["source_url"]) if data.get("source_url") else None,
            raw_title=str(data["raw_title"]),
            raw_price_cny=raw_price,
            fx_rate_usd_cny=fx_rate,
            target_margin_pct=Decimal(str(data["target_margin_pct"])),
            cost_usd=cost_usd,
            optimized_title_en=str(data["optimized_title_en"]),
            bullet_points=bullets,
            suggested_sku=str(data["suggested_sku"]),
            suggested_category=str(data["suggested_category"]),
            suggested_hs_code=(
                str(data["suggested_hs_code"]) if data.get("suggested_hs_code") else None
            ),
            target_price_usd=Decimal(str(data["target_price_usd"])),
            moq_tiers=tiers,
            images=images,
            attributes=_as_json_dict(data.get("attributes")),
            options_spec=options_spec,
            variants_spec=variants_spec,
            status=status,
            listing_state=draft_listing_state(status, history),
            imported_product_id=(
                UUID(str(imported_product_id)) if imported_product_id else None
            ),
            imported_variant_id=(
                UUID(str(imported_variant_id)) if imported_variant_id else None
            ),
            publish_history=history,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    async def list_drafts(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[SourcingDraftRead]:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM sourcing_drafts WHERE tenant_id = :tid "
                    "ORDER BY created_at DESC, id DESC"
                ),
                {"tid": tenant_id},
            )
        ).all()
        return [self.draft_from_mapping(row._mapping) for row in rows]

    async def _load_draft(
        self, session: AsyncSession, tenant_id: UUID, draft_id: UUID
    ) -> SourcingDraftRead:
        row = (
            await session.execute(
                text("SELECT * FROM sourcing_drafts WHERE tenant_id = :tid AND id = :did"),
                {"tid": tenant_id, "did": draft_id},
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="sourcing-draft-not-found",
                status_code=404,
                code="ERR_SOURCING_DRAFT_NOT_FOUND",
                title="Sourcing Draft Not Found",
                detail=f"Sourcing draft '{draft_id}' does not exist for this tenant.",
            )
        return self.draft_from_mapping(row._mapping)

    async def delete_draft(
        self, session: AsyncSession, tenant_id: UUID, actor_id: str, draft_id: UUID
    ) -> SourcingDeleteResult:
        deleted_id = await session.scalar(
            text(
                "DELETE FROM sourcing_drafts WHERE tenant_id = :tid AND id = :did RETURNING id"
            ),
            {"tid": tenant_id, "did": draft_id},
        )
        if deleted_id is None:
            raise AppError(
                slug="sourcing-draft-not-found",
                status_code=404,
                code="ERR_SOURCING_DRAFT_NOT_FOUND",
                title="Sourcing Draft Not Found",
                detail=f"Sourcing draft '{draft_id}' does not exist for this tenant.",
            )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sourcing_draft.deleted",
            resource_type="sourcing_draft",
            resource_id=draft_id,
        )
        return SourcingDeleteResult(deleted=True, draft_id=draft_id)

    # -- import ----------------------------------------------------------------------

    async def _ensure_product(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        draft: SourcingDraftRead,
    ) -> UUID:
        product = await self.catalog.create_product(
            session,
            tenant_id,
            actor_id,
            ProductCreate(
                spu_code=draft.suggested_sku,
                category=draft.suggested_category or "uncategorized",
                hs_code=draft.suggested_hs_code,
                origin_country="CN",
                i18n_specs={
                    "en": {
                        "title": draft.optimized_title_en,
                        "selling_points": draft.bullet_points,
                        "source_title": draft.raw_title,
                        "images": draft.images,
                    }
                },
                certifications=[],
                ai_faq_facts={"bullet_points": draft.bullet_points},
            ),
        )
        return product.id

    async def _find_existing_product(
        self, session: AsyncSession, tenant_id: UUID, spu_code: str
    ) -> UUID:
        existing = await session.scalar(
            text("SELECT id FROM products WHERE tenant_id = :tid AND spu_code = :spu"),
            {"tid": tenant_id, "spu": spu_code},
        )
        if existing is None:
            raise AppError(
                slug="sourcing-product-unavailable",
                status_code=500,
                code="ERR_SOURCING_PRODUCT_UNAVAILABLE",
                title="Sourcing Product Unavailable",
                detail=f"Product '{spu_code}' could not be created or found.",
            )
        return UUID(str(existing))

    async def _ensure_variant(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        draft: SourcingDraftRead,
        product_id: UUID,
    ) -> UUID:
        variant = await self.catalog.create_variant(
            session,
            tenant_id,
            actor_id,
            VariantCreate(
                product_id=product_id,
                sku=draft.suggested_sku,
                attributes={
                    "sourcing_draft_id": str(draft.id),
                    "source_platform": draft.source_platform,
                    "source_url": draft.source_url,
                    "images": draft.images,
                },
                cost_price_base=draft.raw_price_cny.quantize(
                    _COST_SCALE, rounding=ROUND_HALF_UP
                ),
                base_currency="CNY",
                min_margin_floor_pct=_DEFAULT_MIN_MARGIN_FLOOR_PCT,
                retail_price_usd=draft.target_price_usd,
                b2b_moq_tiers=[
                    MoqTierInput(min_qty=tier.min_qty, unit_price_usd=tier.unit_price_usd)
                    for tier in draft.moq_tiers
                ],
                weight_grams=_weight_grams(draft.attributes),
                lead_time_days=_DEFAULT_LEAD_TIME_DAYS,
            ),
            role=UserRole.MANAGER,
        )
        return variant.id

    async def _find_existing_variant(
        self, session: AsyncSession, tenant_id: UUID, sku: str
    ) -> UUID:
        existing = await session.scalar(
            text("SELECT id FROM product_variants WHERE tenant_id = :tid AND sku = :sku"),
            {"tid": tenant_id, "sku": sku},
        )
        if existing is None:
            raise AppError(
                slug="sourcing-variant-unavailable",
                status_code=500,
                code="ERR_SOURCING_VARIANT_UNAVAILABLE",
                title="Sourcing Variant Unavailable",
                detail=f"Variant '{sku}' could not be created or found.",
            )
        return UUID(str(existing))

    async def import_to_catalog(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        draft_id: UUID,
    ) -> SourcingImportResult:
        draft = await self._load_draft(session, tenant_id, draft_id)
        if draft.imported_product_id is not None and draft.imported_variant_id is not None:
            return SourcingImportResult(
                draft=draft,
                product_id=draft.imported_product_id,
                variant_id=draft.imported_variant_id,
                sku=draft.suggested_sku,
                retail_price_usd=draft.target_price_usd,
                created=False,
            )
        try:
            product_id = await self._ensure_product(session, tenant_id, actor_id, draft)
        except AppError as exc:
            if exc.code != "ERR_DUPLICATE_SPU":
                raise
            product_id = await self._find_existing_product(
                session, tenant_id, draft.suggested_sku
            )

        variant_id: UUID | None = None
        if draft.variants_spec:
            for item in draft.variants_spec:
                child_sku = f"{draft.suggested_sku}-{item.sku_suffix}"
                retail_usd = item.retail_price_usd or draft.target_price_usd
                try:
                    created_var = await self.catalog.create_variant(
                        session,
                        tenant_id,
                        actor_id,
                        VariantCreate(
                            product_id=product_id,
                            sku=child_sku,
                            attributes={
                                "sourcing_draft_id": str(draft.id),
                                "source_platform": draft.source_platform,
                                "source_url": draft.source_url,
                                "option_values": dict(item.option_values),
                                "image_url": item.image_url,
                                "images": draft.images,
                            },
                            cost_price_base=item.cost_price_cny.quantize(
                                _COST_SCALE, rounding=ROUND_HALF_UP
                            ),
                            base_currency="CNY",
                            min_margin_floor_pct=_DEFAULT_MIN_MARGIN_FLOOR_PCT,
                            retail_price_usd=retail_usd,
                            b2b_moq_tiers=[
                                MoqTierInput(
                                    min_qty=tier.min_qty, unit_price_usd=tier.unit_price_usd
                                )
                                for tier in draft.moq_tiers
                            ],
                            weight_grams=_weight_grams(draft.attributes),
                            lead_time_days=_DEFAULT_LEAD_TIME_DAYS,
                        ),
                        role=UserRole.MANAGER,
                    )
                    vid = created_var.id
                except AppError as exc:
                    if exc.code != "ERR_DUPLICATE_SKU":
                        raise
                    vid = await self._find_existing_variant(session, tenant_id, child_sku)
                if variant_id is None:
                    variant_id = vid
        if variant_id is None:
            try:
                variant_id = await self._ensure_variant(
                    session, tenant_id, actor_id, draft, product_id
                )
            except AppError as exc:
                if exc.code != "ERR_DUPLICATE_SKU":
                    raise
                variant_id = await self._find_existing_variant(
                    session, tenant_id, draft.suggested_sku
                )

        await CatalogMediaService().attach_draft_assets_to_product(
            session,
            tenant_id,
            sourcing_draft_id=draft_id,
            product_id=product_id,
            variant_id=variant_id,
        )

        row = (
            await session.execute(
                text(
                    """
                    UPDATE sourcing_drafts
                    SET status = 'imported',
                        imported_product_id = :pid,
                        imported_variant_id = :vid,
                        updated_at = now()
                    WHERE tenant_id = :tid AND id = :did
                    RETURNING *
                    """
                ),
                {
                    "pid": product_id,
                    "vid": variant_id,
                    "tid": tenant_id,
                    "did": draft_id,
                },
            )
        ).one()
        updated = self.draft_from_mapping(row._mapping)
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sourcing_draft.imported",
            resource_type="sourcing_draft",
            resource_id=draft_id,
            after_state={
                "product_id": str(product_id),
                "variant_id": str(variant_id),
                "sku": updated.suggested_sku,
            },
        )
        return SourcingImportResult(
            draft=updated,
            product_id=product_id,
            variant_id=variant_id,
            sku=updated.suggested_sku,
            retail_price_usd=updated.target_price_usd,
            created=True,
        )

    # -- publish ---------------------------------------------------------------------

    async def _channel_account(
        self, session: AsyncSession, tenant_id: UUID, channel_account_id: UUID
    ) -> str:
        channel_type = await session.scalar(
            text(
                "SELECT channel_type FROM channel_accounts "
                "WHERE tenant_id = :tid AND id = :caid"
            ),
            {"tid": tenant_id, "caid": channel_account_id},
        )
        if channel_type is None:
            raise AppError(
                slug="channel-account-not-found",
                status_code=404,
                code="ERR_CHANNEL_ACCOUNT_NOT_FOUND",
                title="Channel Account Not Found",
                detail=f"Channel account '{channel_account_id}' does not exist for this tenant.",
            )
        return str(channel_type)

    async def _upsert_mapping(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        variant_id: UUID,
        channel_account_id: UUID,
        external_product_id: str,
        external_sku: str,
        external_variant_id: str | None = None,
        external_inventory_item_id: str | None = None,
        sync_inventory_enabled: bool,
    ) -> UUID:
        mapping_id = await session.scalar(
            text(
                """
                INSERT INTO platform_sku_mappings (
                    id, tenant_id, variant_id, channel_account_id, external_product_id,
                    external_sku, external_variant_id, external_inventory_item_id,
                    bundle_multiplier, sync_inventory_enabled
                ) VALUES (
                    :id, :tid, :vid, :caid, :epid, :esku, :evid, :eiid, 1, :sync
                )
                ON CONFLICT (tenant_id, channel_account_id, external_sku)
                DO UPDATE SET
                    variant_id = EXCLUDED.variant_id,
                    external_product_id = EXCLUDED.external_product_id,
                    external_variant_id = COALESCE(
                        EXCLUDED.external_variant_id,
                        platform_sku_mappings.external_variant_id
                    ),
                    external_inventory_item_id = COALESCE(
                        EXCLUDED.external_inventory_item_id,
                        platform_sku_mappings.external_inventory_item_id
                    ),
                    sync_inventory_enabled = EXCLUDED.sync_inventory_enabled
                RETURNING id
                """
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "vid": variant_id,
                "caid": channel_account_id,
                "epid": external_product_id,
                "esku": external_sku,
                "evid": external_variant_id,
                "eiid": external_inventory_item_id,
                "sync": sync_inventory_enabled,
            },
        )
        if mapping_id is None:
            raise AppError(
                slug="sku-mapping-unavailable",
                status_code=500,
                code="ERR_SKU_MAPPING_UNAVAILABLE",
                title="SKU Mapping Unavailable",
                detail=f"Channel SKU mapping '{external_sku}' could not be persisted.",
            )
        return UUID(str(mapping_id))

    async def _load_product_variants(
        self, session: AsyncSession, tenant_id: UUID, product_id: UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, sku, retail_price_usd, attributes
                    FROM product_variants
                    WHERE tenant_id = :tid AND product_id = :pid
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"tid": tenant_id, "pid": product_id},
            )
        ).all()
        return [dict(row._mapping) for row in rows]

    async def _load_media_blobs(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        product_id: UUID | None,
        sourcing_draft_id: UUID | None = None,
    ) -> list[MarketplaceImageBlob]:
        raw_blobs = await CatalogMediaService().load_asset_blobs(
            session,
            tenant_id,
            product_id=product_id,
            sourcing_draft_id=sourcing_draft_id,
        )
        return [
            MarketplaceImageBlob(
                filename=str(item["filename"]),
                mime_type=str(item["mime_type"]),
                data=bytes(item["data"]),
                alt_text=str(item.get("alt_text") or ""),
            )
            for item in raw_blobs
        ]

    async def _dispatch_to_channels(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        product_id: UUID,
        primary_variant_id: UUID,
        base_sku: str,
        title: str,
        description: str,
        bullet_points: list[str],
        category: str,
        hs_code: str | None,
        moq_tiers: list[SourcingMoqTier],
        images: list[str],
        options: list[SourcingOptionSpec],
        variants: list[dict[str, Any]],
        payload: SourcingPublishRequest,
        listing_publisher: MarketplaceListingPublisher | None,
        sourcing_draft_id: UUID | None = None,
    ) -> list[SourcingPublishResult]:
        image_blobs = await self._load_media_blobs(
            session,
            tenant_id,
            product_id=product_id,
            sourcing_draft_id=sourcing_draft_id,
        )
        sync_inventory_enabled = payload.stock_sync_ratio > 0
        results: list[SourcingPublishResult] = []
        for channel_account_id in payload.channel_account_ids:
            channel_type = await self._channel_account(session, tenant_id, channel_account_id)
            variant_payloads: list[MarketplaceVariantPayload] = []
            for var_row in variants:
                v_sku = str(var_row["sku"])
                v_ext_sku = build_external_sku(channel_type, v_sku)
                v_price = (
                    Decimal(str(var_row["retail_price_usd"])) * payload.price_multiplier
                ).quantize(_CENT, rounding=ROUND_HALF_UP)
                v_attrs = _as_json_dict(var_row.get("attributes"))
                raw_opt_vals = v_attrs.get("option_values")
                option_values = (
                    {str(k): str(v) for k, v in raw_opt_vals.items()}
                    if isinstance(raw_opt_vals, Mapping)
                    else {}
                )
                variant_payloads.append(
                    MarketplaceVariantPayload(
                        local_variant_id=UUID(str(var_row["id"])),
                        local_sku=v_sku,
                        external_sku=v_ext_sku,
                        option_values=option_values,
                        price_usd=v_price,
                        quantity=payload.initial_stock_quantity,
                        image_url=str(v_attrs["image_url"])
                        if v_attrs.get("image_url")
                        else None,
                    )
                )

            primary_ext_sku = (
                variant_payloads[0].external_sku
                if variant_payloads
                else build_external_sku(channel_type, base_sku)
            )
            primary_price_usd = (
                variant_payloads[0].price_usd
                if variant_payloads
                else Decimal("0.01")
            )
            external_product_id = pending_listing_product_id(primary_ext_sku)
            listing_state: Literal["mapping_ready", "live_published"] = "mapping_ready"
            publish_mode: Literal["local_sku_mapping", "live_marketplace_api"] = (
                "local_sku_mapping"
            )
            external_listing_confirmed = False
            remote_status: str | None = None
            receipt_by_sku: dict[str, MarketplaceVariantReceipt] = {}
            is_multi_variant = len(variant_payloads) > 1 or any(
                bool(vp.option_values) for vp in variant_payloads
            )
            if payload.push_to_marketplace:
                if listing_publisher is None:
                    raise AppError(
                        slug="marketplace-publisher-unavailable",
                        status_code=503,
                        code="ERR_MARKETPLACE_PUBLISHER_UNAVAILABLE",
                        title="Marketplace Publisher Unavailable",
                        detail=(
                            "A live marketplace push was requested but no listing publisher "
                            "is wired into this deployment."
                        ),
                    )
                receipt = await listing_publisher.publish_listing(
                    session,
                    tenant_id,
                    MarketplaceListingPayload(
                        channel_account_id=channel_account_id,
                        channel_type=channel_type,
                        external_sku=primary_ext_sku,
                        title=title,
                        description=description,
                        bullet_points=bullet_points,
                        price_usd=primary_price_usd,
                        quantity=payload.initial_stock_quantity,
                        images=images,
                        category=category,
                        hs_code=hs_code,
                        moq_tiers=moq_tiers,
                        options=options if is_multi_variant else [],
                        variants=variant_payloads if is_multi_variant else [],
                        image_blobs=image_blobs,
                    ),
                )
                confirmed_product_id = (receipt.external_product_id or "").strip()
                if not confirmed_product_id:
                    raise AppError(
                        slug="marketplace-publisher-receipt-invalid",
                        status_code=502,
                        code="ERR_MARKETPLACE_PUBLISHER_RECEIPT_INVALID",
                        title="Marketplace Publisher Receipt Invalid",
                        detail=(
                            f"The marketplace push for SKU '{primary_ext_sku}' returned no "
                            "external product id; nothing is recorded as a live listing."
                        ),
                    )
                external_product_id = confirmed_product_id
                listing_state = "live_published"
                publish_mode = "live_marketplace_api"
                external_listing_confirmed = True
                remote_status = receipt.status
                receipt_by_sku = {vr.external_sku: vr for vr in receipt.variant_receipts}

            primary_mapping_id: UUID | None = None
            if variant_payloads:
                for vp in variant_payloads:
                    vr = receipt_by_sku.get(vp.external_sku)
                    mapped_id = await self._upsert_mapping(
                        session,
                        tenant_id=tenant_id,
                        variant_id=vp.local_variant_id or primary_variant_id,
                        channel_account_id=channel_account_id,
                        external_product_id=external_product_id,
                        external_sku=vp.external_sku,
                        external_variant_id=vr.external_variant_id if vr else None,
                        external_inventory_item_id=(
                            vr.external_inventory_item_id if vr else None
                        ),
                        sync_inventory_enabled=sync_inventory_enabled,
                    )
                    if primary_mapping_id is None:
                        primary_mapping_id = mapped_id
            else:
                primary_mapping_id = await self._upsert_mapping(
                    session,
                    tenant_id=tenant_id,
                    variant_id=primary_variant_id,
                    channel_account_id=channel_account_id,
                    external_product_id=external_product_id,
                    external_sku=primary_ext_sku,
                    sync_inventory_enabled=sync_inventory_enabled,
                )

            published_at = datetime.now(UTC)
            results.append(
                SourcingPublishResult(
                    channel_account_id=channel_account_id,
                    channel_type=channel_type,
                    external_sku=primary_ext_sku,
                    external_product_id=external_product_id,
                    price_usd=primary_price_usd,
                    stock_sync_ratio=payload.stock_sync_ratio,
                    mapping_id=primary_mapping_id,
                    published_at=published_at,
                    listing_state=listing_state,
                    external_listing_confirmed=external_listing_confirmed,
                    publish_mode=publish_mode,
                    remote_status=remote_status,
                )
            )
        return results

    async def publish(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        draft_id: UUID,
        payload: SourcingPublishRequest,
        listing_publisher: MarketplaceListingPublisher | None = None,
    ) -> list[SourcingPublishResult]:
        """Publish a draft to channel accounts, locally or through a live marketplace push."""
        draft = await self._load_draft(session, tenant_id, draft_id)
        if draft.imported_variant_id is None or draft.imported_product_id is None:
            imported = await self.import_to_catalog(session, tenant_id, actor_id, draft_id)
            draft = imported.draft
        variant_id = draft.imported_variant_id
        product_id = draft.imported_product_id
        if variant_id is None or product_id is None:
            raise AppError(
                slug="sourcing-variant-unavailable",
                status_code=500,
                code="ERR_SOURCING_VARIANT_UNAVAILABLE",
                title="Sourcing Variant Unavailable",
                detail=f"Draft '{draft_id}' is not linked to a catalog variant.",
            )

        loaded_variants = await self._load_product_variants(session, tenant_id, product_id)
        if not loaded_variants:
            loaded_variants = [
                {
                    "id": variant_id,
                    "sku": draft.suggested_sku,
                    "retail_price_usd": draft.target_price_usd,
                    "attributes": {},
                }
            ]
        results = await self._dispatch_to_channels(
            session,
            tenant_id=tenant_id,
            product_id=product_id,
            primary_variant_id=variant_id,
            base_sku=draft.suggested_sku,
            title=draft.optimized_title_en,
            description=build_listing_description(draft),
            bullet_points=list(draft.bullet_points),
            category=draft.suggested_category,
            hs_code=draft.suggested_hs_code,
            moq_tiers=list(draft.moq_tiers),
            images=list(draft.images),
            options=list(draft.options_spec),
            variants=loaded_variants,
            payload=payload,
            listing_publisher=listing_publisher,
            sourcing_draft_id=draft_id,
        )

        history: list[dict[str, Any]] = list(draft.publish_history)
        for res in results:
            entry: dict[str, Any] = {
                "channel_account_id": str(res.channel_account_id),
                "channel_type": res.channel_type,
                "external_sku": res.external_sku,
                "external_product_id": res.external_product_id,
                "price_usd": str(res.price_usd),
                "stock_sync_ratio": str(res.stock_sync_ratio),
                "published_at": res.published_at.isoformat(),
                "listing_state": res.listing_state,
                "external_listing_confirmed": res.external_listing_confirmed,
                "publish_mode": res.publish_mode,
            }
            if res.remote_status is not None:
                entry["remote_status"] = res.remote_status
            history.append(entry)

        await session.execute(
            text(
                """
                UPDATE sourcing_drafts
                SET status = 'published',
                    publish_history = CAST(:history AS jsonb),
                    updated_at = now()
                WHERE tenant_id = :tid AND id = :did
                """
            ),
            {
                "history": json.dumps(history, default=str),
                "tid": tenant_id,
                "did": draft_id,
            },
        )
        # Only report a live state when *every* requested channel was confirmed remotely.
        live_confirmed = bool(results) and all(
            result.listing_state == "live_published" for result in results
        )
        after_state: dict[str, Any] = {
            "sku": draft.suggested_sku,
            "channels": [str(result.channel_account_id) for result in results],
            "listing_state": "live_published" if live_confirmed else "mapping_ready",
            "external_listing_confirmed": live_confirmed,
        }
        if live_confirmed:
            after_state["publish_mode"] = "live_marketplace_api"
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sourcing_draft.published",
            resource_type="sourcing_draft",
            resource_id=draft_id,
            after_state=after_state,
        )
        return results

    async def get_draft(
        self, session: AsyncSession, tenant_id: UUID, draft_id: UUID
    ) -> SourcingDraftRead:
        return await self._load_draft(session, tenant_id, draft_id)

    async def publish_master_product(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        product_id: UUID,
        payload: SourcingPublishRequest,
        listing_publisher: MarketplaceListingPublisher | None = None,
    ) -> list[SourcingPublishResult]:
        """Publish an existing master catalog product (and all its variants/media) directly."""
        product_row = (
            await session.execute(
                text(
                    """
                    SELECT id, spu_code, category, hs_code, i18n_specs, ai_faq_facts
                    FROM products
                    WHERE tenant_id = :tid AND id = :pid
                    """
                ),
                {"tid": tenant_id, "pid": product_id},
            )
        ).one_or_none()
        if product_row is None:
            raise AppError(
                slug="product-not-found",
                status_code=404,
                code="ERR_PRODUCT_NOT_FOUND",
                title="Product Not Found",
                detail=f"Product '{product_id}' not found.",
            )
        variants = await self._load_product_variants(session, tenant_id, product_id)
        if not variants:
            raise AppError(
                slug="product-variants-empty",
                status_code=422,
                code="ERR_PRODUCT_VARIANTS_EMPTY",
                title="No Variants To Publish",
                detail=f"Product '{product_id}' has no SKU variants to publish.",
            )
        specs = _as_json_dict(product_row.i18n_specs)
        raw_en = specs.get("en")
        en_spec: dict[str, Any] = dict(raw_en) if isinstance(raw_en, Mapping) else {}
        title = str(en_spec.get("title") or product_row.spu_code).strip()
        raw_bullets = (
            en_spec.get("selling_points")
            or _as_json_dict(product_row.ai_faq_facts).get("bullet_points")
            or []
        )
        bullets = [str(b) for b in raw_bullets if str(b).strip()]
        images = [str(u) for u in (en_spec.get("images") or []) if str(u).strip()]
        desc_parts = [title]
        if bullets:
            desc_parts.extend(f"- {b}" for b in bullets)
        if product_row.category:
            desc_parts.append(f"- Category: {product_row.category}")
        if product_row.hs_code:
            desc_parts.append(f"- HS code: {product_row.hs_code}")

        option_map: dict[str, list[str]] = {}
        for var in variants:
            attrs = _as_json_dict(var.get("attributes"))
            opt_vals = attrs.get("option_values")
            if isinstance(opt_vals, Mapping):
                for key, val in opt_vals.items():
                    vals = option_map.setdefault(str(key), [])
                    if str(val) not in vals:
                        vals.append(str(val))
        options = [SourcingOptionSpec(name=k, values=v) for k, v in option_map.items() if v]

        results = await self._dispatch_to_channels(
            session,
            tenant_id=tenant_id,
            product_id=product_id,
            primary_variant_id=UUID(str(variants[0]["id"])),
            base_sku=str(product_row.spu_code),
            title=title,
            description="\n".join(desc_parts),
            bullet_points=bullets,
            category=str(product_row.category or "General Export"),
            hs_code=str(product_row.hs_code) if product_row.hs_code else None,
            moq_tiers=[],
            images=images,
            options=options,
            variants=variants,
            payload=payload,
            listing_publisher=listing_publisher,
            sourcing_draft_id=None,
        )
        live_confirmed = bool(results) and all(
            res.listing_state == "live_published" for res in results
        )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.product.published",
            resource_type="product",
            resource_id=product_id,
            after_state={
                "spu_code": str(product_row.spu_code),
                "channels": [str(r.channel_account_id) for r in results],
                "listing_state": "live_published" if live_confirmed else "mapping_ready",
            },
        )
        return results


__all__ = [
    "DnsResolver",
    "ListingState",
    "MAX_SOURCE_FETCH_BYTES",
    "MarketplaceImageBlob",
    "MarketplaceListingDispatchReceipt",
    "MarketplaceListingPayload",
    "MarketplaceListingPublisher",
    "MarketplaceVariantPayload",
    "MarketplaceVariantReceipt",
    "PENDING_LISTING_PREFIX",
    "PlannedSourcingDraft",
    "SourcingCollectRequest",
    "SourcingDeleteResult",
    "SourcingDraftRead",
    "SourcingImportResult",
    "SourcingMoqTier",
    "SourcingOptionSpec",
    "SourcingPageMetadata",
    "SourcingPricing",
    "SourcingPublishRequest",
    "SourcingPublishResult",
    "SourcingService",
    "SourcingSource",
    "SourcingVariantSpec",
    "build_english_title",
    "build_external_sku",
    "build_listing_description",
    "build_selling_points",
    "build_suggested_sku",
    "channel_sku_prefix",
    "compute_sourcing_pricing",
    "draft_listing_state",
    "parse_page_metadata",
    "parse_supplier_copy",
    "pending_listing_product_id",
    "plan_sourcing_draft",
    "resolve_and_validate_public_url",
    "system_dns_resolver",
    "validate_source_url",
]
