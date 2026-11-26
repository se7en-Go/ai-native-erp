"""Read-only ad spend synchronisation with exact `Decimal` ratio maths.

Design invariants
-----------------
* **Read only.** Every adapter is a `ReadonlyAdsAdapter`: it issues HTTPS `GET`
  requests (or nothing at all, for the sandbox) and can never push a bid, budget
  or creative change back to the ad platform. `read_only is True` is asserted in
  the test suite and the transport stub rejects any non-GET verb.
* **Credentials never leave the envelope.** The token is AES-256-GCM encrypted
  with `EnvelopeCryptoService` bound to
  ``AAD = f"{tenant_id}:{account_id}:ads_oauth"`` and packed into
  `ad_account_connections.encrypted_credentials_b64`. Responses carry only
  `has_credentials` and an opaque masked fingerprint.
* **Money is never a float.** ROAS / TACoS / CTR are computed from `NUMERIC`
  values with `Decimal` and `ROUND_HALF_UP`, quantized to `0.01`, and serialised
  as exact decimal strings.

`erp_core.finance` never imports `erp_core.ai_engine` or `erp_core.catalog`
(`domain_modules_never_import_ai_engine`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.ads_models import AdAccountConnection, AdSpendDailySnapshot
from erp_core.finance.ads_schemas import (
    AdAccountConnect,
    AdAccountRead,
    AdsAccountStatus,
    AdSpendRowRead,
    AdsPlatform,
    AdsSpendSummaryRead,
    AdsSyncResultRead,
)
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.errors import AppError

#: Envelope AAD suffix for ad-platform OAuth / API tokens.
ADS_CREDENTIAL_TYPE = "ads_oauth"
#: Money / ratio quantum - every reported value is exact to two decimals.
CENTS = Decimal("0.01")
#: Longest window a single summary or sync may cover.
MAX_ADS_DAYS = 365
#: Default window for one read-only pull.
DEFAULT_ADS_SYNC_DAYS = 7

_AMAZON_HOST_RE = re.compile(r"^advertising-api(-[a-z]{2,8})?\.amazon\.com$")
_META_HOST_RE = re.compile(r"^graph\.facebook\.com$")
_ENVELOPE_MAGIC = b"AD1"
_ENVELOPE_HEADER_BYTES = 3 + 4 + 12 + 16

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "instance-data"})
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


# --------------------------------------------------------------------------------------
# Errors / helpers
# --------------------------------------------------------------------------------------
def _corrupt_envelope() -> AppError:
    return AppError(
        slug="ads-credential-corrupt",
        title="Ad Account Credential Corrupt",
        status_code=403,
        detail="The stored ad-account credential envelope is malformed.",
        code="ERR_ADS_CREDENTIAL_CORRUPT",
    )


class AdsSpendAdapterError(AppError):
    """A read-only pull failed. The message never contains a token or a URL secret."""

    def __init__(self, *, platform: str, detail: str, code: str = "ERR_ADS_ADAPTER_FAILED"):
        super().__init__(
            slug="ads-adapter-failed",
            title="Ads Adapter Failed",
            status_code=422,
            detail=detail,
            code=code,
        )
        self.platform = platform


def _is_public_host(host: str) -> bool:
    lowered = host.strip().lower().rstrip(".")
    if not lowered or lowered in _BLOCKED_HOSTNAMES:
        return False
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        return "." in lowered
    mapped = getattr(literal, "ipv4_mapped", None)
    candidate = mapped if mapped is not None else literal
    if (
        candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
    ):
        return False
    return not (
        isinstance(candidate, ipaddress.IPv4Address) and candidate in _SHARED_ADDRESS_SPACE
    )


def validate_ad_platform_https_url(raw_url: str, *, host_pattern: re.Pattern[str]) -> str:
    """Allow only `https://<expected-vendor-host>/...` with no credentials in the URL.

    The vendor host pattern is mandatory so an operator-supplied base URL can
    never be pointed at an internal service (SSRF) even if it is https.
    """
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme.lower() != "https":
        raise AdsSpendAdapterError(
            platform="unknown",
            detail="Ad platform endpoints must use https.",
            code="ERR_ADS_ENDPOINT_UNSAFE",
        )
    if parsed.username or parsed.password:
        raise AdsSpendAdapterError(
            platform="unknown",
            detail="Ad platform endpoints must not embed credentials.",
            code="ERR_ADS_ENDPOINT_UNSAFE",
        )
    host = (parsed.hostname or "").strip().lower()
    if not _is_public_host(host) or host_pattern.match(host) is None:
        raise AdsSpendAdapterError(
            platform="unknown",
            detail=f"Host '{host or '(none)'}' is not an allowed ad-platform endpoint.",
            code="ERR_ADS_ENDPOINT_UNSAFE",
        )
    return raw_url.strip()


def decimal_from(value: object) -> Decimal:
    """Coerce a provider scalar into a non-negative `Decimal` quantized to cents."""
    if value is None or value == "":
        return Decimal("0.00")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if not parsed.is_finite() or parsed < 0:
        return Decimal("0.00")
    return parsed.quantize(CENTS, rounding=ROUND_HALF_UP)


def int_from(value: object) -> int:
    """Coerce a provider scalar into a non-negative `int`."""
    if value is None or value == "":
        return 0
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    if not parsed.is_finite() or parsed < 0:
        return 0
    return int(parsed)


def date_from(value: object) -> date | None:
    """Parse `YYYY-MM-DD` (or a full ISO timestamp) into a `date`."""
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return date.fromisoformat(text_value[:10])
    except ValueError:
        return None


def money_str(value: Decimal) -> str:
    """Serialise money as an exact two-decimal string (never a float)."""
    return f"{value.quantize(CENTS, rounding=ROUND_HALF_UP):f}"


def compute_roas(attributed_sales: Decimal, ad_spend: Decimal) -> Decimal:
    """ROAS = attributed_sales / ad_spend, quantized `0.01`; `0.00` when spend is 0."""
    if ad_spend <= 0:
        return Decimal("0.00")
    return (attributed_sales / ad_spend).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_tacos_pct(ad_spend: Decimal, total_store_sales: Decimal) -> Decimal:
    """TACoS = (ad_spend / total_store_sales) * 100; `0.00` when sales are 0."""
    if total_store_sales <= 0:
        return Decimal("0.00")
    return ((ad_spend / total_store_sales) * 100).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_ctr_pct(clicks: int, impressions: int) -> Decimal:
    """CTR = (clicks / impressions) * 100, quantized `0.01`; `0.00` when impressions are 0."""
    if impressions <= 0 or clicks <= 0:
        return Decimal("0.00")
    return ((Decimal(clicks) / Decimal(impressions)) * 100).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )


# --------------------------------------------------------------------------------------
# Credential envelope packing (`encrypted_credentials_b64`)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CredentialEnvelope:
    key_version: int
    nonce: bytes
    auth_tag: bytes
    ciphertext: bytes


def pack_credential_envelope(
    *,
    key_version: int,
    nonce: bytes,
    auth_tag: bytes,
    ciphertext: bytes,
) -> str:
    """Pack the four `EnvelopeCryptoService` parts into one base64 TEXT value."""
    if key_version <= 0 or len(nonce) != 12 or len(auth_tag) != 16 or not ciphertext:
        raise _corrupt_envelope()
    payload = (
        _ENVELOPE_MAGIC
        + struct.pack(">I", key_version)
        + nonce
        + auth_tag
        + ciphertext
    )
    return base64.b64encode(payload).decode("ascii")


def unpack_credential_envelope(encoded: str) -> CredentialEnvelope:
    """Reverse `pack_credential_envelope`, failing closed on a tampered value."""
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise _corrupt_envelope() from exc
    if len(raw) <= _ENVELOPE_HEADER_BYTES or raw[:3] != _ENVELOPE_MAGIC:
        raise _corrupt_envelope()
    key_version = struct.unpack(">I", raw[3:7])[0]
    nonce = raw[7:19]
    auth_tag = raw[19:35]
    ciphertext = raw[35:]
    if key_version <= 0 or not ciphertext:
        raise _corrupt_envelope()
    return CredentialEnvelope(
        key_version=key_version,
        nonce=nonce,
        auth_tag=auth_tag,
        ciphertext=ciphertext,
    )


def masked_token_preview(encrypted_credentials_b64: str) -> str:
    """Opaque, non-reversible fingerprint - never derived from the plaintext token."""
    digest = hashlib.sha256(encrypted_credentials_b64.encode("utf-8")).hexdigest()
    return f"\u2022\u2022\u2022\u2022{digest[:4]}"


# --------------------------------------------------------------------------------------
# Adapter protocol & value objects
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AdSpendRow:
    """One `(spend_date, campaign_name)` fact from a read-only pull."""

    spend_date: date
    campaign_name: str
    ad_spend: Decimal
    attributed_sales: Decimal
    total_store_sales: Decimal
    impressions: int = 0
    clicks: int = 0
    sku: str | None = None
    currency: str = "USD"


@dataclass(frozen=True)
class AdsAccountContext:
    """In-memory account handle. The plaintext token never leaves this object."""

    tenant_id: UUID
    account_id: UUID
    platform: str
    external_account_id: str
    account_name: str
    currency: str
    access_token: str = field(repr=False, default="")

    def __repr__(self) -> str:  # pragma: no cover - repr is asserted, not executed hot
        return (
            f"AdsAccountContext(account_id={self.account_id}, "
            f"platform={self.platform}, account_name={self.account_name!r})"
        )


@runtime_checkable
class ReadonlyAdsAdapter(Protocol):
    """A strictly read-only ads reporting adapter."""

    platform: str
    read_only: bool

    async def fetch_daily_spend(
        self, *, account: AdsAccountContext, days: int
    ) -> Sequence[AdSpendRow]:
        """Pull daily campaign spend. Must never mutate anything on the platform."""
        ...


#: Deterministic sandbox campaign matrix: (campaign name, SKU or None).
SANDBOX_CAMPAIGNS: tuple[tuple[str, str | None], ...] = (
    ("QZ-LED Search 搜索广告", "LED-PANEL-600"),
    ("QZ-LED Retarget 再营销", "LED-TUBE-1200"),
    ("QZ-LED Brand 品牌词", None),
)


class SandboxAdsAdapter:
    """Offline adapter producing deterministic multi-campaign daily rows.

    `total_store_sales` is the **day's whole-store revenue** and is carried on the
    first campaign row only, so summing the column over a window stays exact
    instead of multiplying the day by the campaign count.
    """

    platform = "sandbox_ads"
    read_only = True

    def __init__(self, *, today: date | None = None) -> None:
        self._today = today

    def _day(self) -> date:
        return self._today or datetime.now(UTC).date()

    async def fetch_daily_spend(
        self, *, account: AdsAccountContext, days: int
    ) -> Sequence[AdSpendRow]:
        window = max(1, min(days, MAX_ADS_DAYS))
        today = self._day()
        currency = account.currency or "USD"
        rows: list[AdSpendRow] = []
        for offset in range(window):
            spend_date = today - timedelta(days=offset)
            for campaign_index, (campaign_name, sku) in enumerate(SANDBOX_CAMPAIGNS):
                impressions = 1_200 + (offset * 37 + campaign_index * 211) % 800
                clicks = impressions // 20 + (campaign_index + 1) * 3
                ad_spend = (
                    Decimal(4_500 + offset * 137 + campaign_index * 900) / Decimal(100)
                ).quantize(CENTS, rounding=ROUND_HALF_UP)
                attributed_sales = (
                    ad_spend
                    * (
                        Decimal("2.10")
                        + Decimal(campaign_index) / Decimal(10)
                        + Decimal(offset % 3) / Decimal(100)
                    )
                ).quantize(CENTS, rounding=ROUND_HALF_UP)
                total_store_sales = Decimal("0.00")
                if campaign_index == 0:
                    total_store_sales = (
                        attributed_sales * Decimal("3.00")
                        + Decimal(2_000 + offset * 17)
                    ).quantize(CENTS, rounding=ROUND_HALF_UP)
                rows.append(
                    AdSpendRow(
                        spend_date=spend_date,
                        campaign_name=campaign_name,
                        ad_spend=ad_spend,
                        attributed_sales=attributed_sales,
                        total_store_sales=total_store_sales,
                        impressions=impressions,
                        clicks=clicks,
                        sku=sku,
                        currency=currency,
                    )
                )
        return rows


class MetaAdsReadonlyAdapter:
    """Meta (Facebook) Marketing Insights over HTTPS `GET`, read-only.

    Meta insights expose *attributed* revenue only. When the response does not
    carry an explicit `total_store_sales`, the attributed figure is used as the
    store-revenue denominator so TACoS stays defined instead of blank.
    """

    platform = "meta_ads"
    read_only = True
    BASE_URL = "https://graph.facebook.com"
    API_VERSION = "v20.0"
    _PURCHASE_ACTIONS = frozenset(
        {
            "purchase",
            "offsite_conversion.fb_pixel_purchase",
            "omni_purchase",
        }
    )

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        api_version: str = API_VERSION,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._api_version = api_version

    def build_url(self, account: AdsAccountContext) -> str:
        url = f"{self.BASE_URL}/{self._api_version}/act_{account.external_account_id}/insights"
        return validate_ad_platform_https_url(url, host_pattern=_META_HOST_RE)

    @staticmethod
    def _attributed_sales(entry: dict[str, object]) -> Decimal:
        action_values = entry.get("action_values")
        if not isinstance(action_values, list):
            return Decimal("0.00")
        total = Decimal("0.00")
        for item in action_values:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("action_type") or "")
            if action_type in MetaAdsReadonlyAdapter._PURCHASE_ACTIONS:
                total += decimal_from(item.get("value"))
        return total.quantize(CENTS, rounding=ROUND_HALF_UP)

    async def fetch_daily_spend(
        self, *, account: AdsAccountContext, days: int
    ) -> Sequence[AdSpendRow]:
        if not account.access_token:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="The Meta ad account has no stored access token.",
                code="ERR_ADS_ADAPTER_NOT_CONFIGURED",
            )
        window = max(1, min(days, MAX_ADS_DAYS))
        until = datetime.now(UTC).date()
        since = until - timedelta(days=window - 1)
        params = {
            "level": "campaign",
            "fields": "campaign_name,spend,impressions,clicks,action_values",
            "time_increment": "1",
            "time_range": f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}',
            "access_token": account.access_token,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(self.build_url(account), params=params)
        except httpx.HTTPError as exc:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail=f"Meta insights request failed ({type(exc).__name__}).",
                code="ERR_ADS_ADAPTER_UNREACHABLE",
            ) from exc
        if response.status_code != 200:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail=f"Meta insights returned HTTP {response.status_code}.",
                code="ERR_ADS_ADAPTER_REJECTED",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="Meta insights returned a non-JSON body.",
                code="ERR_ADS_ADAPTER_INVALID_BODY",
            ) from exc
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="Meta insights response has no `data` array.",
                code="ERR_ADS_ADAPTER_INVALID_BODY",
            )
        rows: list[AdSpendRow] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            spend_date = date_from(entry.get("date_start"))
            if spend_date is None:
                continue
            attributed = self._attributed_sales(entry)
            store_sales = entry.get("total_store_sales")
            total_store_sales = (
                decimal_from(store_sales) if store_sales is not None else attributed
            )
            rows.append(
                AdSpendRow(
                    spend_date=spend_date,
                    campaign_name=str(entry.get("campaign_name") or "Meta campaign"),
                    ad_spend=decimal_from(entry.get("spend")),
                    attributed_sales=attributed,
                    total_store_sales=total_store_sales,
                    impressions=int_from(entry.get("impressions")),
                    clicks=int_from(entry.get("clicks")),
                    currency=account.currency or "USD",
                )
            )
        return rows


class AmazonAdsReadonlyAdapter:
    """Amazon Ads campaign reporting over HTTPS `GET`, read-only.

    Amazon exposes reporting as a POST in its v3 API, which a read-only adapter
    must not do, so this adapter consumes the `GET /v2/campaigns/extended`
    reporting projection instead: a plain resource read that returns one record
    per campaign/day with cost, impressions, clicks and attributed sales.
    """

    platform = "amazon_ads"
    read_only = True
    DEFAULT_HOST = "advertising-api.amazon.com"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        host: str = DEFAULT_HOST,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._host = host

    def build_url(self) -> str:
        return validate_ad_platform_https_url(
            f"https://{self._host}/v2/campaigns/extended",
            host_pattern=_AMAZON_HOST_RE,
        )

    async def fetch_daily_spend(
        self, *, account: AdsAccountContext, days: int
    ) -> Sequence[AdSpendRow]:
        if not account.access_token:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="The Amazon ad account has no stored access token.",
                code="ERR_ADS_ADAPTER_NOT_CONFIGURED",
            )
        window = max(1, min(days, MAX_ADS_DAYS))
        until = datetime.now(UTC).date()
        since = until - timedelta(days=window - 1)
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Amazon-Advertising-API-Scope": account.external_account_id,
            "Accept": "application/json",
        }
        params = {"startDate": since.isoformat(), "endDate": until.isoformat()}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    self.build_url(), headers=headers, params=params
                )
        except httpx.HTTPError as exc:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail=f"Amazon Ads request failed ({type(exc).__name__}).",
                code="ERR_ADS_ADAPTER_UNREACHABLE",
            ) from exc
        if response.status_code != 200:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail=f"Amazon Ads returned HTTP {response.status_code}.",
                code="ERR_ADS_ADAPTER_REJECTED",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="Amazon Ads returned a non-JSON body.",
                code="ERR_ADS_ADAPTER_INVALID_BODY",
            ) from exc
        if isinstance(payload, list):
            entries: list[object] = payload
        elif isinstance(payload, dict) and isinstance(payload.get("campaigns"), list):
            entries = list(payload["campaigns"])
        else:
            raise AdsSpendAdapterError(
                platform=self.platform,
                detail="Amazon Ads response has no `campaigns` array.",
                code="ERR_ADS_ADAPTER_INVALID_BODY",
            )
        rows: list[AdSpendRow] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            spend_date = date_from(entry.get("reportDate") or entry.get("date"))
            if spend_date is None:
                continue
            attributed = decimal_from(
                entry.get("attributedSalesSameSku") or entry.get("attributedSales")
            )
            store_sales = entry.get("totalStoreSales")
            rows.append(
                AdSpendRow(
                    spend_date=spend_date,
                    campaign_name=str(entry.get("campaignName") or "Amazon campaign"),
                    ad_spend=decimal_from(entry.get("cost") or entry.get("spend")),
                    attributed_sales=attributed,
                    total_store_sales=(
                        decimal_from(store_sales) if store_sales is not None else attributed
                    ),
                    impressions=int_from(entry.get("impressions")),
                    clicks=int_from(entry.get("clicks")),
                    sku=(
                        str(entry["sku"])
                        if entry.get("sku") not in (None, "")
                        else None
                    ),
                    currency=account.currency or "USD",
                )
            )
        return rows


def default_ads_adapters() -> dict[str, ReadonlyAdsAdapter]:
    """The production read-only adapter registry, one per DB-allowed platform."""
    return {
        "sandbox_ads": SandboxAdsAdapter(),
        "meta_ads": MetaAdsReadonlyAdapter(),
        "amazon_ads": AmazonAdsReadonlyAdapter(),
    }


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------
class AdsSpendService:
    """Connect accounts, pull daily snapshots and report exact ROAS / TACoS."""

    def __init__(self, adapters: dict[str, ReadonlyAdsAdapter] | None = None) -> None:
        self._adapters = adapters if adapters is not None else default_ads_adapters()

    # -- projections -------------------------------------------------------------
    @staticmethod
    def _account_read(row: AdAccountConnection) -> AdAccountRead:
        return AdAccountRead(
            id=row.id,
            tenant_id=row.tenant_id,
            platform=cast(AdsPlatform, row.platform),
            account_name=row.account_name,
            external_account_id=row.external_account_id,
            currency=row.currency,
            status=cast(AdsAccountStatus, row.status),
            last_synced_at=row.last_synced_at,
            created_at=row.created_at,
            has_credentials=bool(row.encrypted_credentials_b64),
            token_preview=masked_token_preview(row.encrypted_credentials_b64),
        )

    @staticmethod
    def _spend_row_read(row: AdSpendDailySnapshot) -> AdSpendRowRead:
        return AdSpendRowRead(
            id=row.id,
            ad_account_id=row.ad_account_id,
            spend_date=row.spend_date,
            campaign_name=row.campaign_name,
            sku=row.sku,
            impressions=row.impressions,
            clicks=row.clicks,
            ad_spend=money_str(row.ad_spend),
            attributed_sales=money_str(row.attributed_sales),
            total_store_sales=money_str(row.total_store_sales),
            currency=row.currency,
        )

    # -- reads -------------------------------------------------------------------
    async def list_accounts(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[AdAccountRead]:
        rows = (
            await session.execute(
                select(AdAccountConnection)
                .where(AdAccountConnection.tenant_id == tenant_id)
                .order_by(AdAccountConnection.created_at.asc(), AdAccountConnection.id.asc())
            )
        ).scalars()
        return [self._account_read(row) for row in rows]

    async def _load_account(
        self, session: AsyncSession, tenant_id: UUID, account_id: UUID
    ) -> AdAccountConnection:
        account = (
            await session.execute(
                select(AdAccountConnection).where(
                    AdAccountConnection.tenant_id == tenant_id,
                    AdAccountConnection.id == account_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise AppError(
                slug="ads-account-not-found",
                title="Ad Account Not Found",
                status_code=404,
                detail="No ad account with that id exists in this workspace.",
                code="ERR_ADS_ACCOUNT_NOT_FOUND",
            )
        return account

    # -- writes ------------------------------------------------------------------
    async def connect_account(
        self,
        session: AsyncSession,
        crypto: EnvelopeCryptoService,
        tenant_id: UUID,
        payload: AdAccountConnect,
    ) -> AdAccountRead:
        """Persist one ad account with its token sealed in an AES-256-GCM envelope."""
        existing = (
            await session.execute(
                select(AdAccountConnection.id).where(
                    AdAccountConnection.tenant_id == tenant_id,
                    AdAccountConnection.platform == payload.platform,
                    AdAccountConnection.external_account_id == payload.external_account_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AppError(
                slug="ads-account-already-connected",
                title="Ad Account Already Connected",
                status_code=409,
                detail=(
                    f"Ad account '{payload.external_account_id}' is already connected for "
                    f"platform '{payload.platform}'."
                ),
                code="ERR_ADS_ACCOUNT_EXISTS",
            )
        account_id = uuid4()
        ciphertext, nonce, auth_tag, key_version = await crypto.encrypt_secret(
            session,
            tenant_id,
            account_id,
            ADS_CREDENTIAL_TYPE,
            payload.access_token,
        )
        account = AdAccountConnection(
            id=account_id,
            tenant_id=tenant_id,
            platform=payload.platform,
            account_name=payload.account_name.strip(),
            external_account_id=payload.external_account_id.strip(),
            encrypted_credentials_b64=pack_credential_envelope(
                key_version=key_version,
                nonce=nonce,
                auth_tag=auth_tag,
                ciphertext=ciphertext,
            ),
            currency=payload.currency,
            status="active",
        )
        session.add(account)
        await session.flush()
        await session.refresh(account)
        return self._account_read(account)

    async def delete_account(
        self, session: AsyncSession, tenant_id: UUID, account_id: UUID
    ) -> None:
        account = await self._load_account(session, tenant_id, account_id)
        await session.delete(account)
        await session.flush()

    async def sync_account(
        self,
        session: AsyncSession,
        crypto: EnvelopeCryptoService,
        tenant_id: UUID,
        account_id: UUID,
        *,
        days: int = DEFAULT_ADS_SYNC_DAYS,
    ) -> AdsSyncResultRead:
        """Pull daily spend read-only and upsert the `(date, campaign)` grain."""
        window = max(1, min(days, MAX_ADS_DAYS))
        account = await self._load_account(session, tenant_id, account_id)
        adapter = self._adapters[account.platform]
        envelope = unpack_credential_envelope(account.encrypted_credentials_b64)
        token = await crypto.decrypt_secret(
            session,
            tenant_id,
            account.id,
            ADS_CREDENTIAL_TYPE,
            envelope.ciphertext,
            envelope.nonce,
            envelope.auth_tag,
            envelope.key_version,
        )
        context = AdsAccountContext(
            tenant_id=tenant_id,
            account_id=account.id,
            platform=account.platform,
            external_account_id=account.external_account_id,
            account_name=account.account_name,
            currency=account.currency,
            access_token=token,
        )
        fetched = list(await adapter.fetch_daily_spend(account=context, days=window))
        if fetched:
            statement = pg_insert(AdSpendDailySnapshot).values(
                [
                    {
                        "id": uuid4(),
                        "tenant_id": tenant_id,
                        "ad_account_id": account.id,
                        "spend_date": row.spend_date,
                        "campaign_name": row.campaign_name,
                        "sku": row.sku,
                        "impressions": row.impressions,
                        "clicks": row.clicks,
                        "ad_spend": row.ad_spend,
                        "attributed_sales": row.attributed_sales,
                        "total_store_sales": row.total_store_sales,
                        "currency": row.currency or account.currency or "USD",
                    }
                    for row in fetched
                ]
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_ad_spend_daily_snapshots_grain",
                set_={
                    "sku": statement.excluded.sku,
                    "impressions": statement.excluded.impressions,
                    "clicks": statement.excluded.clicks,
                    "ad_spend": statement.excluded.ad_spend,
                    "attributed_sales": statement.excluded.attributed_sales,
                    "total_store_sales": statement.excluded.total_store_sales,
                    "currency": statement.excluded.currency,
                },
            )
            await session.execute(statement)
        synced_at = datetime.now(UTC)
        account.last_synced_at = synced_at
        account.status = "active"
        await session.flush()
        await session.refresh(account)
        return AdsSyncResultRead(
            account=self._account_read(account),
            rows_upserted=len(fetched),
            days=window,
            synced_at=synced_at,
        )

    # -- summary -----------------------------------------------------------------
    async def spend_summary(
        self, session: AsyncSession, tenant_id: UUID, *, days: int = 30
    ) -> AdsSpendSummaryRead:
        """Windowed totals plus exact ROAS / TACoS / CTR derived with `Decimal`."""
        window = max(1, min(days, MAX_ADS_DAYS))
        cutoff = datetime.now(UTC).date() - timedelta(days=window - 1)
        snapshots = list(
            (
                await session.execute(
                    select(AdSpendDailySnapshot)
                    .where(
                        AdSpendDailySnapshot.tenant_id == tenant_id,
                        AdSpendDailySnapshot.spend_date >= cutoff,
                    )
                    .order_by(
                        AdSpendDailySnapshot.spend_date.desc(),
                        AdSpendDailySnapshot.campaign_name.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        total_ad_spend = Decimal("0.00")
        total_attributed = Decimal("0.00")
        total_impressions = 0
        total_clicks = 0
        #: Whole-store revenue is a *day* fact: take the max per date, never the sum
        #: across campaigns, so TACoS cannot be multiplied by the campaign count.
        store_sales_by_date: dict[date, Decimal] = {}
        currency = "USD"
        for snapshot in snapshots:
            total_ad_spend += snapshot.ad_spend
            total_attributed += snapshot.attributed_sales
            total_impressions += snapshot.impressions
            total_clicks += snapshot.clicks
            previous = store_sales_by_date.get(snapshot.spend_date)
            if previous is None or snapshot.total_store_sales > previous:
                store_sales_by_date[snapshot.spend_date] = snapshot.total_store_sales
            if snapshot.currency:
                currency = snapshot.currency
        total_store_sales = sum(store_sales_by_date.values(), Decimal("0.00"))

        total_ad_spend = total_ad_spend.quantize(CENTS, rounding=ROUND_HALF_UP)
        total_attributed = total_attributed.quantize(CENTS, rounding=ROUND_HALF_UP)
        total_store_sales = total_store_sales.quantize(CENTS, rounding=ROUND_HALF_UP)
        return AdsSpendSummaryRead(
            tenant_id=tenant_id,
            days=window,
            currency=currency,
            total_ad_spend=money_str(total_ad_spend),
            total_attributed_sales=money_str(total_attributed),
            total_store_sales=money_str(total_store_sales),
            total_impressions=total_impressions,
            total_clicks=total_clicks,
            roas=money_str(compute_roas(total_attributed, total_ad_spend)),
            tacos_pct=money_str(compute_tacos_pct(total_ad_spend, total_store_sales)),
            ctr_pct=money_str(compute_ctr_pct(total_clicks, total_impressions)),
            rows=[self._spend_row_read(snapshot) for snapshot in snapshots],
        )
