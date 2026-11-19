from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis

from erp_core.shared.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)


async def get_redis(request: Request) -> Redis:
    client: Redis = request.app.state.redis
    return client
