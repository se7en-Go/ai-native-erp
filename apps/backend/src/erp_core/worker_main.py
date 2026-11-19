from __future__ import annotations

import asyncio
import logging
import signal

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

# The worker writes order rows outside FastAPI, so its mapping needs every table the
# ORM foreign keys reference, exactly like api_main registers them.
import erp_core.tenancy.models  # noqa: F401
from erp_core.channels.ingress_worker import (
    register_channel_worker_tasks,
    run_webhook_worker_loop,
)
from erp_core.channels.webhook_delivery import drain_deliveries
from erp_core.orders.reconciliation_worker import reconcile_ecommerce_orders_cron
from erp_core.orders.tracking_worker import monitor_stalled_shipments_cron, process_17track_webhook
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
)
from erp_core.shared.redis_client import create_redis_client

logger = logging.getLogger(__name__)
_settings = get_settings()
broker = RedisStreamBroker(url=_settings.REDIS_URL)
schedule_source = ListRedisScheduleSource(url=_settings.REDIS_URL)
scheduler = TaskiqScheduler(broker=broker, sources=[schedule_source, LabelScheduleSource(broker)])
broker.task(schedule=[{"cron": "0 * * * *"}])(monitor_stalled_shipments_cron)
broker.task(schedule=[{"cron": "*/15 * * * *"}])(reconcile_ecommerce_orders_cron)
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


async def serve() -> None:
    """Run the webhook stream loop until SIGINT or SIGTERM, then close resources."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("webhook worker loop starting")
    try:
        await run_webhook_worker_loop(
            _session_factory,
            _redis,
            stop,
            broker_session_factory=_broker_session_factory,
        )
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
