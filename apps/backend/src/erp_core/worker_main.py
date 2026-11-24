from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import text
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

import erp_core.ai_engine.models  # noqa: F401
import erp_core.billing.models  # noqa: F401
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401

# The worker publishes due social posts outside FastAPI, so its mapping needs the social
# tables too (the ORM foreign keys from social_posts/social_accounts must resolve).
import erp_core.social.models  # noqa: F401

# The worker writes order rows outside FastAPI, so its mapping needs every table the
# ORM foreign keys reference, exactly like api_main registers them.
import erp_core.tenancy.models  # noqa: F401
from erp_core.channels.adapters.marketplace_im import is_marketplace_im_event
from erp_core.channels.ecommerce.inventory_sync_dispatcher import (
    drain_inventory_sync_all_tenants,
    reconcile_inventory_sync_cron,
)
from erp_core.channels.ecommerce.shopify import process_shopify_webhook
from erp_core.channels.ingress_worker import (
    process_raw_webhook_event,
    register_channel_worker_tasks,
    run_webhook_worker_loop,
)
from erp_core.channels.webhook_delivery import SessionFactory, drain_deliveries
from erp_core.channels.webhook_store import set_raw_webhook_processing_state
from erp_core.orders.reconciliation_worker import reconcile_ecommerce_orders_cron
from erp_core.orders.tracking_worker import monitor_stalled_shipments_cron, process_17track_webhook
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
)
from erp_core.shared.redis_client import create_redis_client
from erp_core.social.scheduler import dispatch_due_social_posts_cron

logger = logging.getLogger(__name__)

# Dedicated provider consumers (Shopify / 17TRACK / storefront platforms) used to be
# only registered by the live E2E fixture, so their Redis streams grew without bound
# in production. This module now owns that topology: one ingress loop plus one
# commerce/tracking drain loop, both running until SIGINT/SIGTERM.
COMMERCE_TRACKING_CHANNELS: tuple[str, ...] = (
    "17track",
    "shopify",
    "woocommerce",
    "shopline",
    "shoplazza",
    "tiktok_shop",
    "shopee",
    "lazada",
    "temu",
    "shein",
    "aliexpress",
    "ozon",
    "walmart",
    "mercado_libre",
)
COMMERCE_DRAIN_INTERVAL_SECONDS = 2.0
ECOMMERCE_RECONCILE_INTERVAL_SECONDS = 15 * 60.0
INVENTORY_SYNC_RECONCILE_INTERVAL_SECONDS = 30 * 60.0
STALLED_SHIPMENT_MONITOR_INTERVAL_SECONDS = 60 * 60.0
SOCIAL_DISPATCH_INTERVAL_SECONDS = 60.0

_settings = get_settings()
broker = RedisStreamBroker(url=_settings.REDIS_URL)
schedule_source = ListRedisScheduleSource(url=_settings.REDIS_URL)
scheduler = TaskiqScheduler(broker=broker, sources=[schedule_source, LabelScheduleSource(broker)])
broker.task(schedule=[{"cron": "0 * * * *"}])(monitor_stalled_shipments_cron)
broker.task(schedule=[{"cron": "*/15 * * * *"}])(reconcile_ecommerce_orders_cron)
broker.task(schedule=[{"cron": "*/30 * * * *"}])(reconcile_inventory_sync_cron)
# Due scheduled social posts are dispatched every minute: `scheduled_at` is minute-granular.
broker.task(schedule=[{"cron": "* * * * *"}])(dispatch_due_social_posts_cron)
broker.task(process_17track_webhook)
_runtime_engine = create_runtime_engine(_settings)
_session_factory = create_session_factory(_runtime_engine)
_broker_engine = create_migrator_engine(_settings)
_broker_session_factory = create_session_factory(_broker_engine)
_redis = create_redis_client(_settings)
register_channel_worker_tasks(broker, _session_factory, _redis, _broker_session_factory)


@broker.task(schedule=[{"cron": "* * * * *"}])
async def drain_17track_webhooks() -> int:
    return await drain_deliveries(
        _broker_session_factory, _redis, "17track", process_17track_webhook
    )


@broker.task
async def worker_ping() -> str:
    return "pong"


async def _handle_storefront_webhook_delivery(
    raw_event_id: str,
    *,
    broker_session_factory: SessionFactory | None = None,
    runtime_session_factory: SessionFactory | None = None,
    redis: Redis | None = None,
) -> int:
    """Acknowledge a storefront/marketplace webhook receipt so its stream cannot pile up.

    ``drain_deliveries`` hands the consumer nothing but the persisted
    ``raw_webhook_events.id``. Order materialization for WooCommerce, Shopline,
    Shoplazza, TikTok Shop and the eight global marketplaces (Shopee, Lazada, Temu, SHEIN,
    AliExpress, Ozon, Walmart, Mercado Libre) is done by the scheduled
    ``reconcile_ecommerce_orders_cron`` pull, exactly as for Shopify order topics.

    Buyer-IM deliveries on those same endpoints are a different story: a marketplace chat
    message is customer-service work, not an order feed, so the receipt is handed to the
    ingress worker, which claims it and writes customers, conversations and messages into
    the tenant's Inbox. The hand-off happens *before* any status write so the receipt is
    claimed exactly once.
    """
    factory = broker_session_factory or _broker_session_factory
    try:
        receipt_id = UUID(raw_event_id)
    except (TypeError, ValueError):
        logger.warning("storefront webhook delivery has non-UUID receipt id: %r", raw_event_id)
        return 0

    channel_type: str | None = None
    delegate_to_inbox = False
    async with system_session(factory) as session:
        row = (
            await session.execute(
                text(
                    "SELECT channel_type, headers, raw_payload FROM raw_webhook_events "
                    "WHERE id = :id AND processing_status IN ('received', 'queued')"
                ),
                {"id": receipt_id},
            )
        ).one_or_none()
        if row is None:
            return 0
        channel_type = str(row[0])
        stored_headers = dict(row[1] or {}) if len(row) > 1 else {}
        stored_payload = dict(row[2] or {}) if len(row) > 2 else {}
        if is_marketplace_im_event(channel_type, stored_payload, stored_headers):
            delegate_to_inbox = True
        else:
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=receipt_id,
                status="processed",
            )

    if delegate_to_inbox:
        # process_raw_webhook_event claims the row itself and marks it processed/pending
        # retry/dead-letter, so marking it here first would swallow the IM turn.
        outcome = await process_raw_webhook_event(
            session_factory=runtime_session_factory or _session_factory,
            redis=redis or _redis,
            raw_event_id=receipt_id,
            broker_session_factory=factory,
        )
        logger.info(
            "marketplace IM webhook routed to inbox channel=%s receipt_id=%s outcome=%s",
            channel_type,
            receipt_id,
            outcome,
        )
        return 1

    logger.info(
        "storefront webhook receipt acknowledged channel=%s receipt_id=%s",
        channel_type,
        receipt_id,
    )
    return 1


def commerce_tracking_handlers(
    broker_session_factory: SessionFactory,
    *,
    runtime_session_factory: SessionFactory | None = None,
    redis: Redis | None = None,
) -> dict[str, Callable[[str], Awaitable[int]]]:
    """Map each dedicated provider stream to the consumer that drains it."""
    storefront = {
        channel: functools.partial(
            _handle_storefront_webhook_delivery,
            broker_session_factory=broker_session_factory,
            runtime_session_factory=runtime_session_factory,
            redis=redis,
        )
        for channel in (
            "woocommerce",
            "shopline",
            "shoplazza",
            "tiktok_shop",
            "shopee",
            "lazada",
            "temu",
            "shein",
            "aliexpress",
            "ozon",
            "walmart",
            "mercado_libre",
        )
    }
    return {
        "17track": process_17track_webhook,
        # process_shopify_webhook loads the verified receipt and applies it through
        # process_shopify_webhook_event inside the owning tenant's RLS context.
        "shopify": process_shopify_webhook,
        **storefront,
    }


async def drain_commerce_and_tracking_once(
    broker_session_factory: SessionFactory,
    redis: Redis,
    *,
    runtime_session_factory: SessionFactory | None = None,
) -> dict[str, int]:
    """Drain one pass for every dedicated commerce/tracking stream.

    A failing channel never starves the others: the exception is logged and that
    channel reports ``0`` for this pass.
    """
    handlers = commerce_tracking_handlers(
        broker_session_factory, runtime_session_factory=runtime_session_factory, redis=redis
    )
    counts: dict[str, int] = {}
    for channel in COMMERCE_TRACKING_CHANNELS:
        handler = handlers.get(channel)
        if handler is None:  # pragma: no cover - defensive against channel drift
            counts[channel] = 0
            continue
        try:
            counts[channel] = await drain_deliveries(
                broker_session_factory, redis, channel, handler
            )
        except Exception:
            logger.exception("commerce/tracking drain failed channel=%s", channel)
            counts[channel] = 0
    return counts


async def _drain_commerce_and_tracking_pass(
    broker_session_factory: SessionFactory,
    redis: Redis,
    runtime_session_factory: SessionFactory,
) -> dict[str, int]:
    """Forward the runtime factory only when one was supplied explicitly.

    Keeps the two-argument drain seam working for callers that predate the marketplace
    IM hand-off (the ingress worker then falls back to the module runtime factory).
    """
    if runtime_session_factory is _session_factory:
        return await drain_commerce_and_tracking_once(broker_session_factory, redis)
    return await drain_commerce_and_tracking_once(
        broker_session_factory, redis, runtime_session_factory=runtime_session_factory
    )


def _periodic_crons_enabled() -> bool:
    raw = os.environ.get("WORKER_ENABLE_PERIODIC_CRONS", "true")
    return raw.strip().lower() not in {"false", "0", "no", "off"}


async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> bool:
    """Sleep up to ``timeout``; return True when the worker was asked to stop."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _run_commerce_and_tracking_loop(
    stop: asyncio.Event,
    broker_session_factory: SessionFactory,
    redis: Redis,
    *,
    runtime_session_factory: SessionFactory | None = None,
    drain_interval: float = COMMERCE_DRAIN_INTERVAL_SECONDS,
    reconcile_interval: float = ECOMMERCE_RECONCILE_INTERVAL_SECONDS,
    inventory_reconcile_interval: float = INVENTORY_SYNC_RECONCILE_INTERVAL_SECONDS,
    stalled_interval: float = STALLED_SHIPMENT_MONITOR_INTERVAL_SECONDS,
    social_interval: float = SOCIAL_DISPATCH_INTERVAL_SECONDS,
    periodic_crons: bool | None = None,
) -> None:
    """Drain commerce/tracking streams, push inventory outbox, and run periodic crons."""
    loop = asyncio.get_running_loop()
    crons = _periodic_crons_enabled() if periodic_crons is None else periodic_crons
    next_reconcile = loop.time() + reconcile_interval
    next_inventory_reconcile = loop.time() + inventory_reconcile_interval
    next_stalled = loop.time() + stalled_interval
    next_social = loop.time() + social_interval
    rt_factory = runtime_session_factory or _session_factory
    logger.info("commerce/tracking worker loop starting (periodic_crons=%s)", crons)
    while not stop.is_set():
        try:
            counts = await _drain_commerce_and_tracking_pass(
                broker_session_factory, redis, rt_factory
            )
            drained = {channel: count for channel, count in counts.items() if count}
            if drained:
                logger.info("commerce/tracking drained %s", drained)
        except Exception:
            # Redis/DB hiccups must never kill the loop: retry on the next tick.
            logger.exception("commerce/tracking drain pass crashed; retrying")
        try:
            inv_totals = await drain_inventory_sync_all_tenants(
                broker_session_factory, rt_factory
            )
            if inv_totals.get("dispatched"):
                logger.info("inventory outbox drained %s", inv_totals)
        except Exception:
            logger.exception("inventory outbox drain pass crashed; retrying")
        if crons:
            now = loop.time()
            if now >= next_reconcile:
                try:
                    reconciled = await reconcile_ecommerce_orders_cron()
                    logger.info("ecommerce order reconcile: %s", reconciled)
                except Exception:
                    logger.exception("ecommerce order reconcile cron failed")
                next_reconcile = now + reconcile_interval
            if now >= next_inventory_reconcile:
                try:
                    inv_reconciled = await reconcile_inventory_sync_cron()
                    logger.info("inventory sync reconcile: %s", inv_reconciled)
                except Exception:
                    logger.exception("inventory sync reconcile cron failed")
                next_inventory_reconcile = now + inventory_reconcile_interval
            if now >= next_stalled:
                try:
                    alerts = await monitor_stalled_shipments_cron()
                    logger.info("stalled shipment monitor alerts: %s", alerts)
                except Exception:
                    logger.exception("stalled shipment monitor cron failed")
                next_stalled = now + stalled_interval
            if now >= next_social:
                try:
                    dispatched = await dispatch_due_social_posts_cron()
                    logger.info("scheduled social dispatch: %s", dispatched)
                except Exception:
                    # A platform outage must never kill the worker loop.
                    logger.exception("scheduled social dispatch cron failed")
                next_social = now + social_interval
        if await _wait_for_stop(stop, drain_interval):
            break
    logger.info("commerce/tracking worker loop stopped")


async def serve() -> None:
    """Run the ingress plus commerce/tracking loops until SIGINT or SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("webhook worker loop starting")
    tasks = [
        asyncio.create_task(
            run_webhook_worker_loop(
                _session_factory,
                _redis,
                stop,
                broker_session_factory=_broker_session_factory,
            ),
            name="webhook-ingress-loop",
        ),
        asyncio.create_task(
            _run_commerce_and_tracking_loop(stop, _broker_session_factory, _redis),
            name="commerce-tracking-loop",
        ),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # One loop died: stop the sibling before surfacing the failure.
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await _redis.aclose()
        await _runtime_engine.dispose()
        await _broker_engine.dispose()
        logger.info("webhook worker loop stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
