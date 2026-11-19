"""Run the stock ingress loop plus the dedicated delivery consumers used by live E2E."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import partial

import httpx

from erp_core.channels.ecommerce.shopify import SHOPIFY_API_PATH, process_shopify_webhook
from erp_core.channels.webhook_delivery import drain_deliveries
from erp_core.orders.tracking_worker import process_17track_webhook
from erp_core.shared.config import get_settings
from erp_core.shared.db import create_migrator_engine, create_session_factory
from erp_core.shared.redis_client import create_redis_client
from erp_core.worker_main import serve


async def run_provider_consumers() -> None:
    settings = get_settings()
    redis = create_redis_client(settings)
    engine = create_migrator_engine(settings)
    factory = create_session_factory(engine)
    shop = f"{os.environ['E2E_RUN_SLUG']}.myshopify.com"

    async def route_shopify(request: httpx.Request) -> None:
        # Route only the fixture shop's Admin API to its loopback HTTP provider.
        if request.url.host != shop or request.url.path != SHOPIFY_API_PATH:
            raise RuntimeError("Live fixture refused a non-demo Shopify upstream request")
        request.url = request.url.copy_with(
            scheme="http", host="127.0.0.1", port=int(os.environ["E2E_PROVIDER_PORT"])
        )
        request.headers["Host"] = request.url.netloc.decode()

    try:
        async with httpx.AsyncClient(
            event_hooks={"request": [route_shopify]}, trust_env=False
        ) as http:
            while True:
                await drain_deliveries(
                    factory,
                    redis,
                    "shopify",
                    partial(process_shopify_webhook, http_client=http),
                    external_account_id=shop,
                )
                await drain_deliveries(factory, redis, "17track", process_17track_webhook)
                await asyncio.sleep(0.2)
    finally:
        await engine.dispose()
        await redis.aclose()


async def main() -> None:
    async with asyncio.TaskGroup() as group:
        consumers = group.create_task(run_provider_consumers())
        await serve()
        consumers.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
