from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from redis.asyncio import Redis

from erp_core.finance.schemas import LiveFxRateItem, LiveFxRatesRead

_FX_CACHE_TTL_SECONDS = 3600  # Refresh hourly from daily/live FX feeds
_FX_STALE_TTL_SECONDS = 86400 * 7
_QUANT_4 = Decimal("0.0001")
_TARGET_QUOTES = ("CNY", "EUR", "GBP", "JPY", "HKD")

_FALLBACK_USD_RATES: dict[str, Decimal] = {
    "CNY": Decimal("7.2400"),
    "EUR": Decimal("0.9200"),
    "GBP": Decimal("0.7900"),
    "JPY": Decimal("154.0000"),
    "HKD": Decimal("7.7800"),
}


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_QUANT_4, rounding=ROUND_HALF_UP)


def _parse_decimal_rate(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        dec = Decimal(str(raw))
    except Exception:
        return None
    if not dec.is_finite() or dec <= Decimal("0"):
        return None
    return _q4(dec)


class LiveFxRateService:
    """Fetches and caches live/daily FX rates with Decimal precision and graceful fallback."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    async def get_live_rates(
        self,
        redis: Redis | None = None,
        *,
        base: str = "USD",
        force_refresh: bool = False,
    ) -> LiveFxRatesRead:
        normalized_base = base.strip().upper()[:3] or "USD"
        fresh_key = f"fx:live:{normalized_base}"
        stale_key = f"fx:stale:{normalized_base}"

        if redis is not None and not force_refresh:
            cached = await self._read_cache(redis, fresh_key)
            if cached is not None:
                return cached

        fetched = await self._fetch_from_providers(normalized_base)
        if fetched is not None:
            if redis is not None:
                await self._write_cache(redis, fresh_key, fetched, _FX_CACHE_TTL_SECONDS)
                await self._write_cache(redis, stale_key, fetched, _FX_STALE_TTL_SECONDS)
            return fetched

        if redis is not None:
            stale = await self._read_cache(redis, stale_key)
            if stale is not None:
                return stale.model_copy(
                    update={"is_live": False, "source": f"{stale.source} (cached)"}
                )

        return self._build_response(
            base=normalized_base,
            raw_rates=_FALLBACK_USD_RATES,
            source="reference_fallback",
            as_of_date=datetime.now(UTC).date().isoformat(),
            is_live=False,
        )

    async def _fetch_from_providers(self, base: str) -> LiveFxRatesRead | None:
        try:
            return await asyncio.wait_for(self._fetch_providers_inner(base), timeout=3.0)
        except Exception:
            return None

    async def _fetch_providers_inner(self, base: str) -> LiveFxRatesRead | None:
        timeout = httpx.Timeout(2.2, connect=1.2)
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        try:
            # 1) Frankfurter (ECB daily reference rates, free, no API key)
            try:
                resp = await client.get(
                    "https://api.frankfurter.app/latest",
                    params={"from": base, "to": ",".join(_TARGET_QUOTES)},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    parsed = self._extract_rates(data.get("rates"))
                    if "CNY" in parsed:
                        fallback_day = datetime.now(UTC).date().isoformat()
                        return self._build_response(
                            base=base,
                            raw_rates=parsed,
                            source="ECB (Frankfurter)",
                            as_of_date=str(data.get("date") or fallback_day),
                            is_live=True,
                        )
            except Exception:
                pass

            # 2) Open Exchange Rates public endpoint (daily/live, free, no API key)
            try:
                resp = await client.get(f"https://open.er-api.com/v6/latest/{base}")
                if resp.status_code == 200:
                    data = resp.json()
                    parsed = self._extract_rates(data.get("rates"))
                    if "CNY" in parsed:
                        raw_date = str(data.get("time_last_update_utc") or "")
                        as_of = raw_date[:16].strip() or datetime.now(UTC).date().isoformat()
                        return self._build_response(
                            base=base,
                            raw_rates=parsed,
                            source="ExchangeRate-API",
                            as_of_date=as_of,
                            is_live=True,
                        )
            except Exception:
                pass
        finally:
            if own_client:
                await client.aclose()
        return None

    def _extract_rates(self, raw_map: Any) -> dict[str, Decimal]:
        if not isinstance(raw_map, dict):
            return {}
        out: dict[str, Decimal] = {}
        for code in _TARGET_QUOTES:
            dec = _parse_decimal_rate(raw_map.get(code))
            if dec is not None:
                out[code] = dec
        return out

    def _build_response(
        self,
        *,
        base: str,
        raw_rates: dict[str, Decimal],
        source: str,
        as_of_date: str,
        is_live: bool,
    ) -> LiveFxRatesRead:
        usd_cny = raw_rates.get("CNY") or _FALLBACK_USD_RATES["CNY"]
        items: list[LiveFxRateItem] = [
            LiveFxRateItem(pair=f"{base}/CNY", base=base, quote="CNY", rate=_q4(usd_cny))
        ]
        # Add practical export cross-pairs (EUR/CNY, GBP/CNY) computed in Decimal.
        eur_per_base = raw_rates.get("EUR")
        if eur_per_base and eur_per_base > Decimal("0"):
            items.append(
                LiveFxRateItem(
                    pair="EUR/CNY",
                    base="EUR",
                    quote="CNY",
                    rate=_q4(usd_cny / eur_per_base),
                )
            )
        gbp_per_base = raw_rates.get("GBP")
        if gbp_per_base and gbp_per_base > Decimal("0"):
            items.append(
                LiveFxRateItem(
                    pair="GBP/CNY",
                    base="GBP",
                    quote="CNY",
                    rate=_q4(usd_cny / gbp_per_base),
                )
            )
        hkd_per_base = raw_rates.get("HKD")
        if hkd_per_base and hkd_per_base > Decimal("0"):
            items.append(
                LiveFxRateItem(
                    pair="USD/HKD",
                    base=base,
                    quote="HKD",
                    rate=_q4(hkd_per_base),
                )
            )

        return LiveFxRatesRead(
            base=base,
            quote="CNY",
            usd_cny=_q4(usd_cny),
            rates=items,
            source=source,
            as_of_date=as_of_date,
            fetched_at=datetime.now(UTC),
            is_live=is_live,
            refresh_interval_seconds=_FX_CACHE_TTL_SECONDS,
        )

    async def _read_cache(self, redis: Redis, key: str) -> LiveFxRatesRead | None:
        try:
            raw = await redis.get(key)
            if not raw:
                return None
            return LiveFxRatesRead.model_validate(json.loads(raw))
        except Exception:
            return None

    async def _write_cache(
        self,
        redis: Redis,
        key: str,
        value: LiveFxRatesRead,
        ttl_seconds: int,
    ) -> None:
        with contextlib.suppress(Exception):
            await redis.set(key, value.model_dump_json(), ex=ttl_seconds)
