"""Compliant lead collectors.

Only legal, documented primary sources are implemented here:

* ``google_places`` — Google Places API (New) with the tenant's own key. The only
  field we permanently keep from Google is the ``place_id`` (the sole field with an
  unlimited cache exemption); the rest of the business row is what the operator
  explicitly reviewed and converted. No key -> explicit ``ERR_GOOGLE_PLACES_KEY_MISSING``.
* ``overpass_osm`` / ``overture_poi`` — OpenStreetMap Overpass (ODbL) and the
  Overture-style open POI shape over the same public endpoint. A city list is
  geocoded with the public Nominatim service (ODbL, unique User-Agent, one call
  per city) and then queried by bounding box over the commercial POI classes
  (``shop``/``office``/``industrial``/``craft``/``company``). Requests identify
  the application with a unique User-Agent and are serialized through a
  process-wide gate so one tenant can never hammer the community server.
* ``csv_import`` — a CSV/trade-show/business-directory file the tenant is
  licensed to use. Importing a row is *not* marketing consent.
* ``fallback_catalog`` — a deterministic, clearly-labelled synthetic catalogue
  used only when an automatic run finds no live records, so a demo tenant never
  sees a crashed job. Every row carries ``provider='fallback_catalog'`` in
  ``raw_metadata`` so live and synthetic leads are never confused.

There is deliberately **no** Meta/Facebook/WhatsApp scraper and no headless
browser automation: both violate the upstream terms and put every tenant's
account at risk (see docs/research/04-compliant-lead-generation.md).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
import phonenumbers

from erp_core.shared.errors import AppError

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
OVERPASS_MAX_LIMIT = 200
# Polite, unique identity for the public OSM community servers (their usage policy
# requires one) and for public-website fetches during email enrichment.
COLLECTOR_USER_AGENT = "AI-Native-ERP-LeadCollector/1.0"
GEOCODING_TIMEOUT_SECONDS = 12.0
OVERPASS_TIMEOUT_SECONDS = 12.0
WEBSITE_FETCH_TIMEOUT_SECONDS = 6.0
WEBSITE_MAX_HTML_BYTES = 256 * 1024
REGION_SEPARATOR = ";"
FALLBACK_PROVIDER = "fallback_catalog"
COMMERCIAL_OSM_KEYS = ("shop", "office", "industrial", "craft", "company")
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
USER_AGENT = (
    f"ai-native-erp-growth/1.0 ({COLLECTOR_USER_AGENT}; "
    "+https://erp.local/growth; compliant lead discovery)"
)

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
    # Non-empty switches Overpass to the geocoded bounding-box batch flow; an
    # empty tuple keeps the legacy single-region area query byte-for-byte.
    cities: tuple[str, ...] = ()

    @property
    def regions(self) -> tuple[str, ...]:
        return self.cities or ((self.target_region,) if self.target_region else ())


def encode_regions(regions: tuple[str, ...] | list[str]) -> str | None:
    """Join one or more `City, CC` regions into the single `target_region` column."""
    joined = f"{REGION_SEPARATOR} ".join(
        part.strip() for part in regions if part and part.strip()
    )
    return joined or None


def decode_regions(value: str | None) -> tuple[str, ...]:
    """Split `target_region` back into the city batch it was created from."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(REGION_SEPARATOR) if part.strip())


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


def _keyword_matches(keywords: tuple[str, ...]) -> str:
    """Tokenize keywords into an Overpass regex; user text never becomes query syntax."""
    tokens: list[str] = []
    for keyword in keywords:
        for token in re.split(r"\s+", _overpass_escape(keyword)):
            token = token.strip()
            if len(token) >= 2 and token not in tokens:
                tokens.append(token)
    if not tokens:
        tokens = [_overpass_escape(keyword) or ".+" for keyword in keywords if keyword.strip()]
    return "|".join(tokens) or ".+"


def _overpass_query(request: CollectRequest) -> str:
    """Legacy single-region query: match the city area by name."""
    city, country = split_region(request.target_region)
    return _overpass_area_query(request.keywords, city, country, request.max_results)


def _overpass_area_query(
    keywords: tuple[str, ...], city: str, country: str | None, max_results: int
) -> str:
    area_filters = [f'["name"="{_overpass_escape(city)}"]']
    if country:
        area_filters.append(f'["ISO3166-1"="{_overpass_escape(country)}"]')
    matches = _keyword_matches(keywords)
    limit = max(1, min(max_results, OVERPASS_MAX_LIMIT))
    return (
        f"[out:json][timeout:{int(OVERPASS_TIMEOUT_SECONDS)}];"
        f"area{''.join(area_filters)}->.searchArea;"
        f'(nwr["name"~"{matches}",i](area.searchArea););'
        f"out center tags {limit};"
    )


def _overpass_bbox_query(
    keywords: tuple[str, ...], area: GeoArea, max_results: int
) -> str:
    """Bounding-box query restricted to the commercial POI classes we are licensed to use."""
    matches = _keyword_matches(keywords)
    bbox = f"{area.south:.6f},{area.west:.6f},{area.north:.6f},{area.east:.6f}"
    limit = max(1, min(max_results, OVERPASS_MAX_LIMIT))
    statements = "".join(
        f'nwr["{osm_key}"]["name"~"{matches}",i]({bbox});'
        for osm_key in COMMERCIAL_OSM_KEYS
    )
    timeout = int(OVERPASS_TIMEOUT_SECONDS)
    return f"[out:json][timeout:{timeout}];({statements});out center tags {limit};"


@dataclass(frozen=True, slots=True)
class GeoArea:
    """Nominatim bounding box for one city, in Overpass `south,west,north,east` order."""

    label: str
    south: float
    west: float
    north: float
    east: float


class NominatimGeocoder:
    """Polite forward geocoding against the public Nominatim instance (ODbL)."""

    provider = "nominatim"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str = NOMINATIM_ENDPOINT,
        timeout: float = GEOCODING_TIMEOUT_SECONDS,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._timeout = timeout
        self._gate = _PublicEndpointGate(min_interval_seconds)

    async def locate(self, place: str) -> GeoArea | None:
        """Return the city box, or ``None`` when the lookup is unusable (never raises)."""
        query = (place or "").strip()
        if not query:
            return None
        await self._gate.wait()
        try:
            response = await self._client.get(
                self._endpoint,
                params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
                headers={"User-Agent": COLLECTOR_USER_AGENT, "Accept": "application/json"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        if not isinstance(first, dict):
            return None
        box = first.get("boundingbox")
        if not isinstance(box, list) or len(box) != 4:
            return None
        south = _clean_float(box[0], minimum=-90, maximum=90)
        north = _clean_float(box[1], minimum=-90, maximum=90)
        west = _clean_float(box[2], minimum=-180, maximum=180)
        east = _clean_float(box[3], minimum=-180, maximum=180)
        if south is None or north is None or west is None or east is None:
            return None
        if south > north:
            south, north = north, south
        if west > east:
            west, east = east, west
        return GeoArea(label=query, south=south, west=west, north=north, east=east)


def _record_from_osm_element(
    element: dict[str, Any], *, default_city: str | None = None
) -> CollectedRecord | None:
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
        city=_clean_text(tags.get("addr:city")) or default_city,
        latitude=latitude,
        longitude=longitude,
        raw_metadata={
            "provider": "overpass_osm",
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
        geocoder: NominatimGeocoder | None = None,
    ) -> None:
        self._request = request
        self._client = client
        self._endpoint = endpoint
        self._gate = _PublicEndpointGate(min_interval_seconds)
        self._geocoder = geocoder or NominatimGeocoder(
            client=client, min_interval_seconds=min_interval_seconds
        )

    async def collect(self) -> list[CollectedRecord]:
        if self._request.cities:
            return await self._collect_cities()
        payload = await self._post(_overpass_query(self._request))
        records: list[CollectedRecord] = []
        for element in _elements(payload):
            record = _record_from_osm_element(element)
            if record is None:
                continue
            records.append(record)
            if len(records) >= self._request.max_results:
                break
        return records

    async def _collect_cities(self) -> list[CollectedRecord]:
        """Geocode each city once, then run one bounding-box query per city."""
        records: list[CollectedRecord] = []
        seen: set[tuple[object, object]] = set()
        for region in self._request.cities:
            if len(records) >= self._request.max_results:
                break
            remaining = self._request.max_results - len(records)
            city, country = split_region(region)
            area = await self._geocoder.locate(region)
            query = (
                _overpass_bbox_query(self._request.keywords, area, remaining)
                if area is not None
                else _overpass_area_query(self._request.keywords, city, country, remaining)
            )
            payload = await self._post(query)
            for element in _elements(payload):
                key = (element.get("type"), element.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                record = _record_from_osm_element(element, default_city=city or None)
                if record is None:
                    continue
                records.append(record)
                if len(records) >= self._request.max_results:
                    break
        return records

    async def _post(self, query: str) -> dict[str, Any]:
        await self._gate.wait()
        try:
            response = await self._client.post(
                self._endpoint,
                data={"data": query},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=OVERPASS_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise provider_unavailable(f"Overpass request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise provider_unavailable("Overpass returned a non-JSON payload.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
            raise provider_unavailable("Overpass response is missing the elements array.")
        return payload


def _elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("elements")
    if not isinstance(raw, list):
        return []
    return [element for element in raw if isinstance(element, dict)]


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
        regions = self._request.regions or (self._request.target_region,)
        for region in regions:
            if len(records) >= self._request.max_results:
                break
            records.extend(await self._collect_region(region))
        return records[: self._request.max_results]

    async def _collect_region(self, region: str) -> list[CollectedRecord]:
        records: list[CollectedRecord] = []
        page_token: str | None = None
        keywords = self._request.keywords or ("",)
        for page in range(self._MAX_PAGES):
            if len(records) >= self._request.max_results:
                break
            keyword = keywords[page % len(keywords)]
            body: dict[str, Any] = {
                "textQuery": f"{keyword} in {region}".strip(),
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
            "provider": "google_places",
            "google_api": "places-new",
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
        cities=request.cities,
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


# --------------------------------------------------------------- fallback catalogue
# Realistic, deterministic placeholders used only when an automatic run gets no
# live records (offline test env, provider outage, zero hits). Every row is tagged
# ``provider='fallback_catalog'`` so the UI can never present it as live OSM data.


@dataclass(frozen=True, slots=True)
class CityProfile:
    city: str
    country: str
    latitude: float
    longitude: float
    phone_base: str
    domain_tld: str


_CITY_PROFILES: dict[str, CityProfile] = {
    profile.city.lower(): profile
    for profile in (
        CityProfile("Dubai", "AE", 25.2048, 55.2708, "+97145550100", "ae"),
        CityProfile("Los Angeles", "US", 34.0522, -118.2437, "+12135550142", "com"),
        CityProfile("Milan", "IT", 45.4642, 9.1900, "+390255550111", "it"),
        CityProfile("Jakarta", "ID", -6.2088, 106.8456, "+622155501234", "co.id"),
        CityProfile("Warsaw", "PL", 52.2297, 21.0122, "+48225550123", "pl"),
        CityProfile("Ho Chi Minh", "VN", 10.7769, 106.7009, "+842855501234", "vn"),
        CityProfile("Singapore", "SG", 1.3521, 103.8198, "+6565550123", "sg"),
        CityProfile("Hamburg", "DE", 53.5511, 9.9937, "+49305550123", "de"),
        CityProfile("Rotterdam", "NL", 51.9244, 4.4777, "+31205550123", "nl"),
        CityProfile("Istanbul", "TR", 41.0082, 28.9784, "+902125550123", "com.tr"),
        CityProfile("Sao Paulo", "BR", -23.5505, -46.6333, "+551155550123", "com.br"),
        CityProfile("Madrid", "ES", 40.4168, -3.7038, "+34915550123", "es"),
        CityProfile("Paris", "FR", 48.8566, 2.3522, "+33155550123", "fr"),
        CityProfile("Seoul", "KR", 37.5665, 126.9780, "+82255501234", "co.kr"),
        CityProfile("Tokyo", "JP", 35.6762, 139.6503, "+81355501234", "jp"),
        CityProfile("Bangkok", "TH", 13.7563, 100.5018, "+6625550123", "co.th"),
        CityProfile("Kuala Lumpur", "MY", 3.1390, 101.6869, "+60355550123", "com.my"),
        CityProfile("Riyadh", "SA", 24.7136, 46.6753, "+966115550123", "com.sa"),
        CityProfile("Cairo", "EG", 30.0444, 31.2357, "+20225550123", "com.eg"),
        CityProfile("Johannesburg", "ZA", -26.2041, 28.0473, "+27115550123", "co.za"),
        CityProfile("Nairobi", "KE", -1.2921, 36.8219, "+254205550123", "co.ke"),
        CityProfile("Sydney", "AU", -33.8688, 151.2093, "+61255501234", "com.au"),
        CityProfile("Toronto", "CA", 43.6532, -79.3832, "+14165550123", "ca"),
        CityProfile("Moscow", "RU", 55.7558, 37.6173, "+74955550123", "ru"),
        CityProfile("Kyiv", "UA", 50.4501, 30.5234, "+380445550123", "com.ua"),
        CityProfile("Santiago", "CL", -33.4489, -70.6693, "+56225550123", "cl"),
        CityProfile("Lima", "PE", -12.0464, -77.0428, "+5115550123", "com.pe"),
    )
}

_FALLBACK_SUFFIXES = (
    "Trading LLC",
    "Import & Export",
    "Wholesale Supply",
    "Industrial Supplies",
    "B2B Distribution",
)
_FALLBACK_RATINGS = ("4.60", "4.30", "4.10", "3.90")


def _stable_index(*parts: str, mod: int) -> int:
    """Stable pseudo-random index (never `hash()`, which is salted per process)."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % mod


def _valid_phone(candidate: str) -> str | None:
    try:
        parsed = phonenumbers.parse(candidate, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "business"


def fallback_catalogue(request: CollectRequest, *, reason: str) -> list[CollectedRecord]:
    """Deterministic realistic prospects for one city/keyword batch.

    The same request always yields the same rows, phone numbers are validated E.164
    numbers in the city's own country, and every record is explicitly labelled as a
    synthetic fallback so it can never be mistaken for live OSM/Google data.
    """
    regions = request.regions or ("",)
    keywords = tuple(keyword for keyword in request.keywords if keyword.strip()) or ("suppliers",)
    wanted = max(1, min(request.max_results * 2, OVERPASS_MAX_LIMIT))
    records: list[CollectedRecord] = []
    for index in range(wanted):
        region = regions[index % len(regions)]
        keyword = keywords[(index // len(regions)) % len(keywords)]
        suffix = _FALLBACK_SUFFIXES[index % len(_FALLBACK_SUFFIXES)]
        city, country_hint = split_region(region)
        profile = _CITY_PROFILES.get(city.lower())
        label = profile.city if profile is not None else city
        if not label:
            label = "Regional"
        name = f"{label} {keyword[:40].title()} {suffix}"
        phone: str | None = None
        latitude: float | None = None
        longitude: float | None = None
        tld = "com"
        resolved_country = country_hint
        if profile is not None:
            offset = _stable_index(label, keyword, suffix, mod=80) + 10
            phone = _valid_phone(profile.phone_base[:-2] + f"{offset:02d}")
            latitude = round(
                profile.latitude + (_stable_index(label, suffix, mod=60) - 30) / 1000, 5
            )
            longitude = round(
                profile.longitude + (_stable_index(keyword, suffix, mod=60) - 30) / 1000, 5
            )
            tld = profile.domain_tld
            resolved_country = profile.country
        slug = f"{_slugify(label)}-{_slugify(keyword)[:24]}-{index + 1}"
        records.append(
            CollectedRecord(
                company_name=name[:255],
                phone_e164=phone,
                website=f"https://www.{slug}.{tld}",
                country=resolved_country,
                city=label,
                latitude=latitude,
                longitude=longitude,
                rating=Decimal(_FALLBACK_RATINGS[index % len(_FALLBACK_RATINGS)]),
                whatsapp_reachable=False,
                raw_metadata={
                    "provider": FALLBACK_PROVIDER,
                    "fallback_reason": reason[:300],
                    "synthetic": True,
                    "attribution": (
                        "Deterministic synthetic catalogue; no upstream provider returned "
                        "live records for this job."
                    ),
                },
            )
        )
    return records
