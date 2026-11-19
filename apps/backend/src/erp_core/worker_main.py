from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from erp_core.shared.config import get_settings

_settings = get_settings()
broker = RedisStreamBroker(url=_settings.REDIS_URL)
schedule_source = ListRedisScheduleSource(url=_settings.REDIS_URL)
scheduler = TaskiqScheduler(broker=broker, sources=[schedule_source])


@broker.task
async def worker_ping() -> str:
    return "pong"
