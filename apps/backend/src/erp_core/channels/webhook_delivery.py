"""Durable receipt/Redis delivery for dedicated provider webhook workers."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.webhook_store import accept_raw_webhook_event
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_session_factory,
    system_session,
)
from erp_core.shared.errors import AppError
from erp_core.shared.redis_client import create_redis_client

logger = structlog.get_logger(__name__)
type SessionFactory = async_sessionmaker[AsyncSession]


def _stream(channel: str) -> str:
    return f"stream:webhooks:{channel}"


async def accept_delivery(
    request: Request,
    *,
    channel: str,
    external_event_id: str,
    payload: dict[str, Any],
    tenant_id: UUID | None = None,
    external_account_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    """Store only authenticated deliveries; return False when already received.

    Workers and tests may mount only ``state.session_factory``; in that case the
    receipts still go through the migrator role that owns the SECURITY DEFINER path.
    """
    broker_factory: SessionFactory | None = getattr(
        request.app.state, "broker_session_factory", None
    )
    owned_engine = None
    if broker_factory is None:
        owned_engine = create_migrator_engine(get_settings())
        broker_factory = create_session_factory(owned_engine)
    redis: Redis | None = getattr(request.app.state, "redis", None)
    owned_redis = redis is None
    if redis is None:
        redis = create_redis_client(get_settings())
    try:
        async with system_session(broker_factory) as session:
            receipt_id = await accept_raw_webhook_event(
                session,
                event_id=uuid4(),
                tenant_id=tenant_id,
                channel_type=channel,
                external_event_id=external_event_id,
                external_account_id=external_account_id,
                headers_json=json.dumps(headers or {}, separators=(",", ":")),
                payload_json=json.dumps(payload, separators=(",", ":")),
            )
        if receipt_id is None:
            return False
        try:
            await redis.xadd(
                _stream(channel),
                {"raw_event_id": str(receipt_id), "channel_type": channel, "attempt": "1"},
            )
        except RedisError as exc:
            raise AppError(
                slug="webhook-queue-unavailable",
                status_code=503,
                code="ERR_WEBHOOK_QUEUE_UNAVAILABLE",
                title="Webhook Queue Unavailable",
                detail="Delivery stored; please retry.",
            ) from exc
        async with system_session(broker_factory) as session:
            await session.execute(
                text(
                    "UPDATE raw_webhook_events SET processing_status = 'queued' "
                    "WHERE id = :id AND processing_status = 'received'"
                ),
                {"id": receipt_id},
            )
        return True
    finally:
        if owned_redis and redis is not None:
            await redis.aclose()
        if owned_engine is not None:
            await owned_engine.dispose()


async def drain_deliveries(
    broker_factory: SessionFactory,
    redis: Redis,
    channel: str,
    process: Callable[[str], Awaitable[int]],
    *,
    external_account_id: str | None = None,
) -> int:
    # Stream entries are hints, not the durability boundary. Re-read pending SQL
    # receipts to recover the window between SQL commit and XADD (or a Redis restart).
    entries = await redis.xrange(_stream(channel), count=50)
    deliveries: dict[str, list[str]] = {}
    for entry_id, fields in entries or []:
        raw_id = fields.get("raw_event_id") if fields else None
        if isinstance(entry_id, str) and isinstance(raw_id, str):
            deliveries.setdefault(raw_id, []).append(entry_id)
    async with system_session(broker_factory) as session:
        params = {"ch": channel}
        scope = ""
        if external_account_id is not None:
            scope = "AND external_account_id = :account "
            params["account"] = external_account_id
        rows = await session.scalars(
            text(
                "SELECT id FROM raw_webhook_events WHERE channel_type = :ch "
                "AND processing_status IN ('received', 'queued') "
                f"{scope}ORDER BY received_at LIMIT 50"
            ),
            params,
        )
        if external_account_id is not None:
            # Redis keys are provider-wide. Only consume hints belonging to this worker's account.
            deliveries = {str(row_id): deliveries.get(str(row_id), []) for row_id in rows}
        else:
            for row_id in rows:
                deliveries.setdefault(str(row_id), [])
    count = 0
    for raw_id, entry_ids in deliveries.items():
        try:
            await process(raw_id)
        except Exception:
            logger.exception("webhook_delivery_failed", receipt_id=raw_id, channel=channel)
            continue
        if entry_ids:
            await redis.xdel(_stream(channel), *entry_ids)
        count += 1
    return count
