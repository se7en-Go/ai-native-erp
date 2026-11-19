from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.webhook_store import set_raw_webhook_processing_state
from erp_core.orders.tracking_adapter import MAIN_STATUS_MAP, map_17track_status
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
    tenant_session,
)
from erp_core.shared.redis_client import create_redis_client
from erp_core.tenancy.audit import record_audit_event

WISMO_STREAM = "stream:erp:events"
# PostgreSQL owns the durable alert flag. The Redis marker handles a retried SQL
# transaction after Redis accepted XADD (for example, when the SQL commit timed out).
_PUBLISH_ONCE = """
if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('XADD', KEYS[1], '*', unpack(ARGV))
    redis.call('SET', KEYS[2], '1', 'EX', 604800)
end
return 1
"""


@dataclass(frozen=True, slots=True)
class ProactiveWismoEvent:
    tenant_id: UUID
    shipment_id: UUID
    order_id: UUID
    tracking_number: str
    standardized_status: str
    sub_status: str | None
    trigger_reason: str

    def fields(self) -> dict[str, str]:
        return {
            "event_type": "PROACTIVE_WISMO_OUTREACH",
            "tenant_id": str(self.tenant_id),
            "shipment_id": str(self.shipment_id),
            "order_id": str(self.order_id),
            "tracking_number": self.tracking_number,
            "standardized_status": self.standardized_status,
            "sub_status": self.sub_status or "",
            "trigger_reason": self.trigger_reason,
        }


async def detect_stalled_shipments_for_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    redis_client: Redis | None = None,
    now: datetime | None = None,
) -> list[ProactiveWismoEvent]:
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=72)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, order_id, tracking_number, standardized_status, sub_status
                FROM shipments
                WHERE tenant_id = :tid AND NOT exception_alert_sent
                  AND (
                    standardized_status IN ('alert', 'undelivered')
                    OR (standardized_status = 'in_transit' AND last_event_at <= :cutoff)
                  )
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"tid": tenant_id, "cutoff": cutoff},
        )
    ).all()
    events = []
    for row in rows:
        event = ProactiveWismoEvent(
            tenant_id=tenant_id,
            shipment_id=row.id,
            order_id=row.order_id,
            tracking_number=row.tracking_number,
            standardized_status=row.standardized_status,
            sub_status=row.sub_status,
            trigger_reason=(
                "in_transit_stalled_72h"
                if row.standardized_status == "in_transit"
                else "status_alert"
            ),
        )
        fields = event.fields()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="tracking_stalled_monitor",
            action="shipment.proactive_wismo_outreach",
            resource_type="shipment",
            resource_id=row.id,
            after_state=fields,
        )
        if redis_client is not None:
            await redis_client.eval(
                _PUBLISH_ONCE,
                2,
                WISMO_STREAM,
                f"erp:wismo:published:{row.id}",
                *(field for pair in fields.items() for field in pair),
            )
        await session.execute(
            text(
                "UPDATE shipments SET exception_alert_sent = true "
                "WHERE tenant_id = :tid AND id = :sid"
            ),
            {"tid": tenant_id, "sid": row.id},
        )
        events.append(event)
    return events


async def list_tracking_tenant_ids(
    directory_factory: async_sessionmaker[AsyncSession],
) -> list[UUID]:
    # app_migrator has an existing read policy for the tenant directory only.
    # Shipment access never uses this connection or a cleared tenant context.
    async with system_session(directory_factory) as session:
        result = await session.execute(text("SELECT id FROM tenants ORDER BY id"))
        return [row.id for row in result]


async def monitor_stalled_shipments_cron() -> int:
    settings = get_settings()
    directory_engine = create_migrator_engine(settings)
    runtime_engine = create_runtime_engine(settings)
    redis_client = create_redis_client(settings)
    try:
        tenant_ids = await list_tracking_tenant_ids(create_session_factory(directory_engine))
        runtime_factory = create_session_factory(runtime_engine)
        total_alerts = 0
        now = datetime.now(UTC)
        for tenant_id in tenant_ids:
            async with tenant_session(runtime_factory, tenant_id) as session:
                total_alerts += len(
                    await detect_stalled_shipments_for_tenant(session, tenant_id, redis_client, now)
                )
        return total_alerts
    finally:
        await redis_client.aclose()
        await runtime_engine.dispose()
        await directory_engine.dispose()


async def apply_tracking_update(
    session: AsyncSession, tenant_id: UUID, shipment_id: UUID, data: dict[str, Any]
) -> bool:
    info = data.get("track_info")
    if not isinstance(info, dict):
        return False
    latest = info.get("latest_status")
    if not isinstance(latest, dict) or str(latest.get("status")) not in MAIN_STATUS_MAP:
        return False
    status, sub = map_17track_status(latest["status"], latest.get("sub_status"))
    event = info.get("latest_event")
    event_at = None
    if isinstance(event, dict):
        raw_time = event.get("time_utc") or event.get("time_iso")
        if isinstance(raw_time, str):
            try:
                parsed = datetime.fromisoformat(raw_time)
                event_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            except ValueError:
                return False
    else:
        event = None

    updated = (
        await session.execute(
            text("""
            UPDATE shipments
            SET standardized_status = :status, sub_status = :sub, last_synced_at = now(),
                last_event_at = COALESCE(:at, last_event_at),
                tracking_events = CASE
                    WHEN tracking_events @> CAST(:event AS jsonb) THEN tracking_events
                    ELSE tracking_events || CAST(:event AS jsonb) END
            WHERE tenant_id = :tid AND id = :sid
              AND (last_event_at IS NULL OR :at >= last_event_at)
              AND (standardized_status <> 'delivered' OR :status = 'delivered')
            RETURNING order_id
        """),
            {
                "tid": tenant_id,
                "sid": shipment_id,
                "status": status,
                "sub": sub,
                "at": event_at,
                "event": json.dumps([event] if event else []),
            },
        )
    ).scalar_one_or_none()
    if updated is None:
        return False
    if status == "delivered":
        # Serialize sibling shipment deliveries before checking the aggregate state.
        await session.execute(
            text("SELECT id FROM orders WHERE tenant_id = :tid AND id = :oid FOR UPDATE"),
            {"tid": tenant_id, "oid": updated},
        )
        await session.execute(
            text("""
                UPDATE orders SET fulfillment_status = 'delivered', updated_at = now()
                WHERE tenant_id = :tid AND id = :oid
                  AND fulfillment_status IN ('shipped', 'partially_shipped')
                  AND NOT EXISTS (SELECT 1 FROM shipments WHERE tenant_id = :tid
                      AND order_id = :oid AND standardized_status <> 'delivered')
            """),
            {"tid": tenant_id, "oid": updated},
        )
    return True


async def _tracking_data_target(
    session_factory: async_sessionmaker[AsyncSession],
    directory_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
) -> tuple[UUID, UUID, dict[str, Any]] | None:
    data = payload.get("data")
    if payload.get("event") != "TRACKING_UPDATED" or not isinstance(data, dict):
        return None
    number = data.get("number")
    if not isinstance(number, str) or not number.strip():
        return None
    target = None
    for tenant_id in await list_tracking_tenant_ids(directory_factory):
        async with tenant_session(session_factory, tenant_id) as session:
            shipment = await session.scalar(
                text(
                    "SELECT id FROM shipments WHERE tenant_id = :tid "
                    "AND tracking_number = :number AND tracking_provider = '17track'"
                ),
                {"tid": tenant_id, "number": number.strip()},
            )
            if shipment is not None:
                if target is not None:
                    # A shared provider webhook cannot establish ownership of a reused number.
                    return None
                target = (tenant_id, shipment, data)
    return target


async def process_17track_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    *,
    directory_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    engine = create_migrator_engine(get_settings()) if directory_factory is None else None
    try:
        if engine is not None:
            directory_factory = create_session_factory(engine)
        assert directory_factory is not None
        target = await _tracking_data_target(session_factory, directory_factory, payload)
        if target is None:
            return 0
        tenant_id, shipment_id, data = target
        async with tenant_session(session_factory, tenant_id) as session:
            return int(await apply_tracking_update(session, tenant_id, shipment_id, data))
    finally:
        if engine is not None:
            await engine.dispose()


async def process_17track_webhook(receipt_id: str) -> int:
    settings = get_settings()
    runtime = create_runtime_engine(settings)
    broker = create_migrator_engine(settings)
    try:
        factory = create_session_factory(runtime)
        broker_factory = create_session_factory(broker)
        raw_id = UUID(receipt_id)
        async with system_session(broker_factory) as session:
            receipt = (
                await session.execute(
                    text(
                        "SELECT raw_payload FROM raw_webhook_events WHERE id = :id "
                        "AND channel_type = '17track' AND signature_verified "
                        "AND processing_status IN ('received', 'queued') FOR UPDATE"
                    ),
                    {"id": raw_id},
                )
            ).one_or_none()
            if receipt is None:
                return 0
            target = await _tracking_data_target(factory, broker_factory, receipt.raw_payload)
            if target is None:
                await set_raw_webhook_processing_state(
                    session,
                    raw_event_id=raw_id,
                    status="dead_letter",
                    error_message="No unambiguous 17TRACK shipment for delivery",
                )
                return 0
            tenant_id, shipment_id, data = target
            async with tenant_session(factory, tenant_id) as tenant:
                updated = await apply_tracking_update(tenant, tenant_id, shipment_id, data)
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=raw_id,
                tenant_id=tenant_id,
                status="processed",
            )
            return int(updated)
    finally:
        await runtime.dispose()
        await broker.dispose()
