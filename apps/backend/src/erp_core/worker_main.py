from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from erp_core.channels.ingress_worker import register_channel_worker_tasks
from erp_core.shared.config import get_settings
from erp_core.shared.db import create_runtime_engine, create_session_factory
from erp_core.shared.redis_client import create_redis_client

_settings = get_settings()
broker = RedisStreamBroker(url=_settings.REDIS_URL)
schedule_source = ListRedisScheduleSource(url=_settings.REDIS_URL)
scheduler = TaskiqScheduler(broker=broker, sources=[schedule_source])
_runtime_engine = create_runtime_engine(_settings)
_session_factory = create_session_factory(_runtime_engine)
_redis = create_redis_client(_settings)
register_channel_worker_tasks(broker, _session_factory, _redis)


@broker.task
async def worker_ping() -> str:
    return "pong"
