from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from erp_core.finance.fx_service import LiveFxRateService


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@pytest.mark.asyncio
async def test_live_fx_fetches_frankfurter_and_caches_in_redis() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "amount": 1.0,
                "base": "USD",
                "date": "2026-11-20",
                "rates": {
                    "CNY": 7.23456,
                    "EUR": 0.92,
                    "GBP": 0.79,
                    "HKD": 7.7812,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    redis = _FakeRedis()
    svc = LiveFxRateService(http_client=client)

    first = await svc.get_live_rates(redis, base="USD")  # type: ignore[arg-type]
    assert first.is_live is True
    assert first.source == "ECB (Frankfurter)"
    assert first.usd_cny == Decimal("7.2346")
    assert first.as_of_date == "2026-11-20"
    assert any(item.pair == "EUR/CNY" for item in first.rates)
    assert len(calls) == 1

    # Second call hits Redis cache without external HTTP call
    second = await svc.get_live_rates(redis, base="USD")  # type: ignore[arg-type]
    assert second.usd_cny == Decimal("7.2346")
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_live_fx_falls_back_to_stale_cache_then_reference() -> None:
    def failing_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream down"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    redis = _FakeRedis()
    svc = LiveFxRateService(http_client=client)

    # No cache yet -> safe reference fallback (never raises 500)
    fallback = await svc.get_live_rates(redis, base="USD")  # type: ignore[arg-type]
    assert fallback.is_live is False
    assert fallback.source == "reference_fallback"
    assert fallback.usd_cny == Decimal("7.2400")
    assert isinstance(fallback.usd_cny, Decimal)
    await client.aclose()


@pytest.mark.asyncio
async def test_live_fx_falls_back_to_secondary_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "frankfurter" in str(request.url):
            return httpx.Response(500)
        payload: dict[str, Any] = {
            "time_last_update_utc": "Fri, 20 Nov 2026 00:00:01 +0000",
            "rates": {"CNY": 7.2111, "EUR": 0.91, "GBP": 0.78, "HKD": 7.77},
        }
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = LiveFxRateService(http_client=client)
    res = await svc.get_live_rates(None, base="USD")
    assert res.is_live is True
    assert res.source == "ExchangeRate-API"
    assert res.usd_cny == Decimal("7.2111")
    await client.aclose()
