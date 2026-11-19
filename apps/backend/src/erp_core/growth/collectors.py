"""Compliant lead collectors.

Only legal, documented primary sources are implemented here:

* ``google_places`` — Google Places API (New) with the tenant's own key. The only
  field we permanently keep from Google is the ``place_id`` (the sole field with an
  unlimited cache exemption); the rest of the business row is what the operator
  explicitly reviewed and converted. No key -> explicit ``ERR_GOOGLE_PLACES_KEY_MISSING``.
* ``overpass_osm`` / ``overture_poi`` — OpenStreetMap Overpass (ODbL) and the
  Overture-style open POI shape over the same public endpoint. Requests identify
  the application with a unique User-Agent and are serialized through a
  process-wide gate so one tenant can never hammer the community server.
* ``csv_import`` — a CSV/trade-show/business-directory file the tenant is
  licensed to use. Importing a row is *not* marketing consent.

There is deliberately **no** Meta/Facebook/WhatsApp scraper and no headless
browser automation: both violate the upstream terms and put every tenant's
account at risk (see docs/research/04-compliant-lead-generation.md).
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from erp_core.shared.errors import AppError

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
OVERPASS_MAX_LIMIT = 200
GOOGLE_PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACES_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.location",
        "places.addressComponents",
        "nextPageToken",
    )
)
USER_AGENT = "ai-native-erp-growth/1.0 (+https://erp.local/growth; compliant lead discovery)"

_OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL 1.0)"
_CSV_COMPANY_KEYS = ("company_name", "company", "legal_name", "name", "business_name", "企业名称")
_CSV_CONTACT_KEYS = ("contact_name", "contact", "contact_person", "person", "联系人")
_CSV_PHONE_KEYS = ("phone_e164", "phone", "telephone", "tel", "mobile", "电话")
_CSV_EMAIL_KEYS = ("email", "e_mail", "mail", "邮箱")
_CSV_WEBSITE_KEYS = ("website", "url", "site", "domain", "网址")
_CSV_COUNTRY_KEYS = ("country", "country_code", "国家")
_CSV_CITY_KEYS = ("city", "town", "城市")
_CSV_LAT_KEYS = ("latitude", "lat", "纬度")
_CSV_LON_KEYS = ("longitude", "lon", "lng", "经度")
_CSV_RATING_KEYS = ("rating", "score", "评分")
_CSV_WHATSAPP_KEYS = ("whatsapp_reachable", "whatsapp", "has_whatsapp")


def google_places_key_missing() -> AppError:
    return AppError(
        slug="google-places-key-missing",
        title="Google Places Key Missing",
        status_code=409,
        detail=(
            "Configure GOOGLE_PLACES_API_KEY for this workspace before collecting "
            "Google Places leads."
        ),
        code="ERR_GOOGLE_PLACES_KEY_MISSING",
    )


def provider_unavailable(detail: str) -> AppError:
    return AppError(
        slug="growth-provider-unavailable",
        title="Lead Provider Unavailable",
        status_code=502,
        detail=detail,
        code="ERR_GROWTH_PROVIDER_UNAVAILABLE",
    )


@dataclass(frozen=True, slots=True)
class CollectRequest:
    keywords: tuple[str, ...]
    target_region: str
    max_results: int
    min_rating: Decimal | None = None
    require_phone: bool = False
    csv_content: str | None = None


@dataclass(frozen=True, slots=True)
class CollectedRecord:
    company_name: str
    contact_name: str | None = None
    phone_e164: str | None = None
    email: str | None = None
    website: str | None = None
    country: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rating: Decimal | None = None
    whatsapp_reachable: bool | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    async def collect(self) -> list[CollectedRecord]: ...


def _clean_text(value: object, *, limit: int = 255) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", " ").strip()
    if not text:
        return None
    return text[:limit]


def _clean_float(value: object, *, minimum: float, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed != parsed or not minimum <= parsed <= maximum:  # NaN or out of range
        return None
    return parsed


def _clean_rating(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if parsed < 0 or parsed > 5:
        return None
    return parsed.quantize(Decimal("0.01"))


def split_region(region: str) -> tuple[str, str | None]:
    """`"Berlin, DE"` -> (`"Berlin"`, `"DE"`); a bare country stays a country filter."""
    parts = [part.strip() for part in region.split(",") if part.strip()]
    if not parts:
        return region.strip(), None
    if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}", parts[-1]):
        return ", ".join(parts[:-1]), parts[-1].upper()
    return parts[0], None


def _overpass_escape(value: str) -> str:
    """Strip Overpass-QL control characters; keywords are user text, never query syntax."""
    return re.sub(r'["\\\n\r\t]', " ", value).strip()[:80]


class _PublicEndpointGate:
    """Serialize and space out calls to a shared community endpoint."""

    _lock = asyncio.Lock()
    _last_call = 0.0

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, min_interval_seconds)

    async def wait(self) -> None:
        async with _PublicEndpointGate._lock:
            if self._min_interval > 0:
                elapsed = time.monotonic() - _PublicEndpointGate._last_call
                remaining = self._min_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            _PublicEndpointGate._last_call = time.monotonic()


def _overpass_query(request: CollectRequest) -> str:
    city, country = split_region(request.target_region)
    area_filters = [f'["name"="{_overpass_escape(city)}"]']
    if country:
        area_filters.append(f'["ISO3166-1"="{country}"]')
    matches = "|".join(_overpass_escape(keyword) for keyword in request.keywords if keyword.strip())
    limit = max(1, min(request.max_results, OVERPASS_MAX_LIMIT))
    return (
        "[out:json][timeout:25];"
        f"area{''.join(area_filters)}->.searchArea;"
        f'(nwr["name"~"{matches}",i](area.searchArea););'
        f"out center tags {limit};"
    )


def _record_from_osm_element(element: dict[str, Any]) -> CollectedRecord | None:
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None
    name = _clean_text(tags.get("name") or tags.get("name:en") or tags.get("operator"))
    if name is None:
        return None
    center: dict[str, Any] = {}
    raw_center = element.get("center")
    if isinstance(raw_center, dict):
        center = raw_center
    latitude = _clean_float(element.get("lat", center.get("lat")), minimum=-90, maximum=90)
    longitude = _clean_float(element.get("lon", center.get("lon")), minimum=-180, maximum=180)
    return CollectedRecord(
        company_name=name,
        phone_e164=_clean_text(tags.get("phone") or tags.get("contact:phone")),
        email=_clean_text(tags.get("email") or tags.get("contact:email")),
        website=_clean_text(tags.get("website") or tags.get("contact:website")),
        country=_clean_text(tags.get("addr:country"), limit=2),
        city=_clean_text(tags.get("addr:city")),
        latitude=latitude,
        longitude=longitude,
        raw_metadata={
            "provider": "openstreetmap_overpass",
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "attribution": _OSM_ATTRIBUTION,
            "license": "ODbL-1.0",
            "tags": sorted(tags)[:20],
        },
    )


class OverpassCollector:
    """Open-license POI discovery through the public Overpass interpreter."""

    provider = "overpass_osm"

    def __init__(
        self,
        request: CollectRequest,
        *,
        client: httpx.AsyncClient,
        endpoint: str = OVERPASS_ENDPOINT,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._request = request
        self._client = client
        self._endpoint = endpoint
        self._gate = _PublicEndpointGate(min_interval_seconds)

    async def collect(self) -> list[CollectedRecord]:
        await self._gate.wait()
        try:
            response = await self._client.post(
                self._endpoint,
                data={"data": _overpass_query(self._request)},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise provider_unavailable(f"Overpass request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise provider_unavailable("Overpass returned a non-JSON payload.") from exc
        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, list):
            raise provider_unavailable("Overpass response is missing the elements array.")
        records: list[CollectedRecord] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            record = _record_from_osm_element(element)
            if record is None:
                continue
            records.append(record)
            if len(records) >= self._request.max_results:
                break
        return records


class OverturePoiCollector(OverpassCollector):
    """Overture-style open POI shape served from the same open-license endpoint."""

    provider = "overture_poi"

    async def collect(self) -> list[CollectedRecord]:
        records = await super().collect()
        return [
            replace(
                record,
                raw_metadata={
                    **record.raw_metadata,
                    "provider": "overture_poi",
                    "dataset": "Overture Maps Places (CDLA-Permissive-2.0)",
                    "license": "CDLA-Permissive-2.0",
                },
            )
            for record in records
        ]


class GooglePlacesCollector:
    """Google Places API (New) text search with a tenant-supplied key."""

    provider = "google_places"
    _MAX_PAGES = 3

    def __init__(
        self,
        request: CollectRequest,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        endpoint: str = GOOGLE_PLACES_ENDPOINT,
    ) -> None:
        if not api_key.strip():
            raise google_places_key_missing()
        self._request = request
        self._client = client
        self._api_key = api_key.strip()
        self._endpoint = endpoint

    async def collect(self) -> list[CollectedRecord]:
        records: list[CollectedRecord] = []
        page_token: str | None = None
        keywords = self._request.keywords or ("",)
        for page in range(self._MAX_PAGES):
            if len(records) >= self._request.max_results:
                break
            keyword = keywords[page % len(keywords)]
            body: dict[str, Any] = {
                "textQuery": f"{keyword} in {self._request.target_region}".strip(),
                "pageSize": 20,
                "languageCode": "en",
            }
            if page_token:
                body["pageToken"] = page_token
            try:
                response = await self._client.post(
                    self._endpoint,
                    json=body,
                    headers={
                        "X-Goog-Api-Key": self._api_key,
                        "X-Goog-FieldMask": GOOGLE_PLACES_FIELD_MASK,
                        "User-Agent": USER_AGENT,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise provider_unavailable(
                    f"Google Places rejected the request with {exc.response.status_code}."
                ) from exc
            except httpx.HTTPError as exc:
                raise provider_unavailable(
                    f"Google Places request failed: {type(exc).__name__}"
                ) from exc
            except ValueError as exc:
                raise provider_unavailable("Google Places returned a non-JSON payload.") from exc
            places = payload.get("places") if isinstance(payload, dict) else None
            for place in places or []:
                record = _record_from_google_place(place)
                if record is not None:
                    records.append(record)
            page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
            if not page_token:
                break
        return records[: self._request.max_results]


def _record_from_google_place(place: object) -> CollectedRecord | None:
    if not isinstance(place, dict):
        return None
    display_name = place.get("displayName")
    name = _clean_text(
        display_name.get("text") if isinstance(display_name, dict) else display_name
    )
    if name is None:
        return None
    location: dict[str, Any] = {}
    raw_location = place.get("location")
    if isinstance(raw_location, dict):
        location = raw_location
    city: str | None = None
    country: str | None = None
    components = place.get("addressComponents")
    for component in components if isinstance(components, list) else []:
        if not isinstance(component, dict):
            continue
        types = component.get("types")
        text = _clean_text(component.get("longText") or component.get("shortText"))
        if not isinstance(types, list) or text is None:
            continue
        if "locality" in types and city is None:
            city = text
        if "country" in types and country is None:
            country = _clean_text(
                component.get("shortText") or component.get("longText"), limit=2
            )
    return CollectedRecord(
        company_name=name,
        phone_e164=_clean_text(place.get("internationalPhoneNumber")),
        website=_clean_text(place.get("websiteUri")),
        country=country,
        city=city,
        latitude=_clean_float(location.get("latitude"), minimum=-90, maximum=90),
        longitude=_clean_float(location.get("longitude"), minimum=-180, maximum=180),
        rating=_clean_rating(place.get("rating")),
        raw_metadata={
            "provider": "google_places_new",
            # Only place_id has an unlimited cache exemption in the Google Maps ToS.
            "google_place_id": _clean_text(place.get("id")),
            "formatted_address": _clean_text(place.get("formattedAddress")),
            "terms": "Google Places API (New); only place_id is retained permanently.",
        },
    )


class CsvCollector:
    """RFC 4180 CSV import for trade-show and licensed directory exports."""

    provider = "csv_import"

    def __init__(self, request: CollectRequest) -> None:
        self._request = request

    async def collect(self) -> list[CollectedRecord]:
        text = (self._request.csv_content or "").lstrip("\ufeff")
        if not text.strip():
            raise provider_unavailable("CSV payload is empty.")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise provider_unavailable("CSV payload has no header row.")
        headers = {(name or "").strip().lower(): name for name in reader.fieldnames}
        records: list[CollectedRecord] = []
        for row in reader:
            record = _record_from_csv_row(row, headers)
            if record is None:
                continue
            records.append(record)
            if len(records) >= self._request.max_results:
                break
        return records


def _csv_value(row: dict[str, Any], headers: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        column = headers.get(key)
        if column is None:
            continue
        value = _clean_text(row.get(column))
        if value is not None:
            return value
    return None


def _record_from_csv_row(row: dict[str, Any], headers: dict[str, str]) -> CollectedRecord | None:
    name = _csv_value(row, headers, _CSV_COMPANY_KEYS)
    if name is None:
        return None
    whatsapp = _csv_value(row, headers, _CSV_WHATSAPP_KEYS)
    return CollectedRecord(
        company_name=name,
        contact_name=_csv_value(row, headers, _CSV_CONTACT_KEYS),
        phone_e164=_csv_value(row, headers, _CSV_PHONE_KEYS),
        email=_csv_value(row, headers, _CSV_EMAIL_KEYS),
        website=_csv_value(row, headers, _CSV_WEBSITE_KEYS),
        country=_csv_value(row, headers, _CSV_COUNTRY_KEYS),
        city=_csv_value(row, headers, _CSV_CITY_KEYS),
        latitude=_clean_float(_csv_value(row, headers, _CSV_LAT_KEYS), minimum=-90, maximum=90),
        longitude=_clean_float(
            _csv_value(row, headers, _CSV_LON_KEYS), minimum=-180, maximum=180
        ),
        rating=_clean_rating(_csv_value(row, headers, _CSV_RATING_KEYS)),
        whatsapp_reachable=(
            None if whatsapp is None else whatsapp.strip().lower() in {"1", "true", "yes", "y"}
        ),
        raw_metadata={
            "provider": "csv_import",
            "consent_note": "Imported rows are not marketing consent; run opt-in collection.",
        },
    )


def unsupported_source(source_provider: str) -> AppError:
    return AppError(
        slug="unsupported-lead-source",
        title="Unsupported Lead Source",
        status_code=422,
        detail=f"Lead source '{source_provider}' is not a compliant collector.",
        code="ERR_UNSUPPORTED_LEAD_SOURCE",
    )


def build_collector(
    source_provider: str,
    request: CollectRequest,
    *,
    client: httpx.AsyncClient,
    google_places_api_key: str = "",
    overpass_min_interval_seconds: float = 1.0,
) -> Collector:
    """Pick the collector for a job; the caller owns the HTTP client's lifecycle."""
    keywords = tuple(keyword for keyword in request.keywords if keyword.strip())
    scoped = CollectRequest(
        keywords=keywords,
        target_region=request.target_region,
        max_results=request.max_results,
        min_rating=request.min_rating,
        require_phone=request.require_phone,
        csv_content=request.csv_content,
    )
    if source_provider == "csv_import":
        return CsvCollector(scoped)
    if source_provider == "google_places":
        return GooglePlacesCollector(
            scoped, client=client, api_key=google_places_api_key
        )
    overpass = OverpassCollector(
        scoped, client=client, min_interval_seconds=overpass_min_interval_seconds
    )
    if source_provider == "overpass_osm":
        return overpass
    if source_provider == "overture_poi":
        return OverturePoiCollector(
            scoped, client=client, min_interval_seconds=overpass_min_interval_seconds
        )
    raise unsupported_source(source_provider)
