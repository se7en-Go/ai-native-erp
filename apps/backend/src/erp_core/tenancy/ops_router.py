"""Admin operations health endpoint: database latency, queues, queues depth, alerts.

`infra/importlinter.ini` (`tenancy_is_foundational`) forbids `erp_core.tenancy`
from importing any downstream domain module, so this router deliberately owns
no domain models. It reads:

* PostgreSQL through the caller's tenant-scoped runtime session (`SELECT 1`,
  `alembic_version` via the migrator factory, and raw counts over
  `raw_webhook_events` / `notification_deliveries` / `inventory_sync_jobs` /
  `dead_letter_events`, all of which are RLS-scoped by the active tenant).
* Redis through `request.app.state.redis` (`PING`, `INFO memory`, and bounded
  `SCAN` + `XLEN` over the Streams this deployment actually consumes).

Everything is read-only and bounded: no `KEYS`, no unbounded fan-out, and a
single failing probe degrades one section instead of failing the request.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.config import Settings
from erp_core.shared.db import system_session
from erp_core.shared.enums import UserRole
from erp_core.shared.observability import get_observability_registry
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

#: Streams this deployment consumes even when they are currently empty.
KNOWN_STREAMS: Final[tuple[str, ...]] = (
    "stream:outbound:messages",
    "stream:erp:events",
)
#: Bounded discovery: a misconfigured Redis must never turn a health probe into a scan storm.
STREAM_SCAN_PATTERN: Final[str] = "*stream*"
STREAM_SCAN_MAX_KEYS: Final[int] = 50
STREAM_SCAN_BATCH: Final[int] = 100

OpsStatus = Literal["ok", "warn", "critical"]

Db = Annotated[AsyncSession, Depends(get_tenant_db)]
OpsAdmin = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]


class OpsStreamRead(BaseModel):
    stream: str
    length: int = Field(ge=0)
    pending: int | None = Field(default=None, ge=0)
    consumers: int | None = Field(default=None, ge=0)


class OpsPostgresRead(BaseModel):
    status: Literal["ok", "critical"]
    latency_ms: float = Field(ge=0)
    alembic_version: str | None = None
    raw_webhook_status_counts: dict[str, int] = Field(default_factory=dict)
    notification_deliveries_pending: int = Field(default=0, ge=0)
    inventory_sync_jobs_pending: int = Field(default=0, ge=0)
    dead_letter_events: int = Field(default=0, ge=0)


class OpsRedisRead(BaseModel):
    status: Literal["ok", "critical"]
    latency_ms: float = Field(ge=0)
    used_memory_bytes: int | None = Field(default=None, ge=0)
    used_memory_human: str | None = None
    connected_clients: int | None = Field(default=None, ge=0)


class OpsHealthRead(BaseModel):
    status: OpsStatus
    checked_at: datetime
    queue_warn_depth: int = Field(ge=1)
    queue_critical_depth: int = Field(ge=1)
    max_backlog: int = Field(ge=0)
    alerts: list[str] = Field(default_factory=list)
    postgres: OpsPostgresRead
    redis: OpsRedisRead
    streams: list[OpsStreamRead] = Field(default_factory=list)


def compute_ops_status(
    *,
    postgres_ok: bool,
    redis_ok: bool,
    max_backlog: int,
    failed_events: int,
    warn_depth: int,
    critical_depth: int,
    backlog_source: str | None = None,
) -> tuple[OpsStatus, list[str]]:
    """Deterministic threshold ladder shared by the endpoint and its tests."""
    alerts: list[str] = []
    status: OpsStatus = "ok"
    source = f" on '{backlog_source}'" if backlog_source else ""
    if not postgres_ok:
        alerts.append("PostgreSQL probe failed: orders, inbox and billing are unavailable.")
        status = "critical"
    if not redis_ok:
        alerts.append("Redis probe failed: streams, rate limiters and caches are unavailable.")
        status = "critical"
    if max_backlog >= max(1, critical_depth):
        alerts.append(
            f"Queue backlog {max_backlog}{source} reached the critical depth {critical_depth}."
        )
        status = "critical"
    elif max_backlog >= max(1, warn_depth):
        alerts.append(f"Queue backlog {max_backlog}{source} exceeded the warn depth {warn_depth}.")
        if status == "ok":
            status = "warn"
    if failed_events > 0:
        alerts.append(f"{failed_events} dead-lettered event(s) are waiting for operator replay.")
        if status == "ok":
            status = "warn"
    return status, alerts


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _alembic_version(request: Request, session: AsyncSession) -> str | None:
    """Read the applied revision; `app_runtime` has no grant, so fall back to the migrator."""
    try:
        # SAVEPOINT: a missing grant must not abort the caller's tenant transaction.
        async with session.begin_nested():
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        return None if revision is None else str(revision)
    except SQLAlchemyError:
        pass
    factory = getattr(request.app.state, "broker_session_factory", None)
    if factory is None:
        return None
    try:
        async with system_session(factory) as migrator_session:
            revision = await migrator_session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        return None if revision is None else str(revision)
    except SQLAlchemyError:
        return None


async def _count(session: AsyncSession, sql: str, tenant_id: str) -> int:
    try:
        async with session.begin_nested():
            return _int(await session.scalar(text(sql), {"tid": tenant_id}))
    except SQLAlchemyError:
        return 0


async def _probe_postgres(
    request: Request, session: AsyncSession, tenant_id: Any
) -> OpsPostgresRead:
    tid = str(tenant_id)
    started = time.perf_counter()
    healthy = True
    try:
        async with session.begin_nested():
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        healthy = False
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    if not healthy:
        return OpsPostgresRead(status="critical", latency_ms=latency_ms)

    raw_counts: dict[str, int] = {}
    try:
        async with session.begin_nested():
            rows = (
                await session.execute(
                    text(
                        "SELECT processing_status, count(*) FROM raw_webhook_events "
                        "WHERE tenant_id = :tid "
                        "GROUP BY processing_status ORDER BY processing_status"
                    ),
                    {"tid": tid},
                )
            ).all()
        raw_counts = {_text(row[0]): _int(row[1]) for row in rows}
    except SQLAlchemyError:
        raw_counts = {}

    return OpsPostgresRead(
        status="ok",
        latency_ms=latency_ms,
        alembic_version=await _alembic_version(request, session),
        raw_webhook_status_counts=raw_counts,
        notification_deliveries_pending=await _count(
            session,
            "SELECT count(*) FROM notification_deliveries "
            "WHERE tenant_id = :tid AND status IN ('pending', 'retrying')",
            tid,
        ),
        inventory_sync_jobs_pending=await _count(
            session,
            "SELECT count(*) FROM inventory_sync_jobs "
            "WHERE tenant_id = :tid AND status IN ('pending', 'processing')",
            tid,
        ),
        dead_letter_events=await _count(
            session,
            "SELECT count(*) FROM dead_letter_events "
            "WHERE tenant_id = :tid AND replayed_at IS NULL",
            tid,
        ),
    )


def _is_stream_key(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("stream:") or "stream" in lowered


async def _discover_streams(redis: Any) -> list[str]:
    names: set[str] = set(KNOWN_STREAMS)
    cursor = 0
    scanned = 0
    try:
        while True:
            cursor, batch = await redis.scan(
                cursor=cursor, match=STREAM_SCAN_PATTERN, count=STREAM_SCAN_BATCH
            )
            for raw_name in batch:
                name = _text(raw_name)
                if _is_stream_key(name):
                    names.add(name)
            scanned += STREAM_SCAN_BATCH
            if cursor in (0, "0") or len(names) >= STREAM_SCAN_MAX_KEYS or scanned >= 1000:
                break
    except Exception:  # noqa: BLE001 - a degraded probe must not fail the endpoint
        pass
    return sorted(names)[:STREAM_SCAN_MAX_KEYS]


async def _stream_backlogs(redis: Any) -> list[OpsStreamRead]:
    registry = get_observability_registry()
    streams: list[OpsStreamRead] = []
    for name in await _discover_streams(redis):
        try:
            length = _int(await redis.xlen(name))
        except Exception:  # noqa: BLE001 - key vanished or is not a stream
            registry.set_stream_backlog(name, 0)
            continue
        pending: int | None = None
        consumers: int | None = None
        try:
            groups = await redis.xinfo_groups(name)
        except Exception:  # noqa: BLE001 - no consumer group yet
            groups = []
        if groups:
            pending = sum(_int(group.get("pending")) for group in groups)
            consumers = sum(_int(group.get("consumers")) for group in groups)
        registry.set_stream_backlog(name, length)
        streams.append(
            OpsStreamRead(stream=name, length=length, pending=pending, consumers=consumers)
        )
    return streams


async def _probe_redis(request: Request) -> tuple[OpsRedisRead, list[OpsStreamRead]]:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return OpsRedisRead(status="critical", latency_ms=0.0), []
    started = time.perf_counter()
    try:
        await redis.ping()
    except Exception:  # noqa: BLE001 - reported, never raised
        return (
            OpsRedisRead(
                status="critical", latency_ms=round((time.perf_counter() - started) * 1000, 3)
            ),
            [],
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    used_memory: int | None = None
    used_memory_human: str | None = None
    connected_clients: int | None = None
    try:
        info = await redis.info("memory")
        if isinstance(info, dict):
            used_memory = _int(info.get("used_memory"))
            human = info.get("used_memory_human")
            used_memory_human = None if human is None else _text(human)
    except Exception:  # noqa: BLE001 - optional detail
        pass
    try:
        clients = await redis.info("clients")
        if isinstance(clients, dict) and "connected_clients" in clients:
            connected_clients = _int(clients.get("connected_clients"))
    except Exception:  # noqa: BLE001 - optional detail
        pass
    return (
        OpsRedisRead(
            status="ok",
            latency_ms=latency_ms,
            used_memory_bytes=used_memory,
            used_memory_human=used_memory_human,
            connected_clients=connected_clients,
        ),
        await _stream_backlogs(redis),
    )


@router.get("/health", response_model=OpsHealthRead)
async def ops_health(
    request: Request,
    principal: OpsAdmin,
    session: Db,
) -> OpsHealthRead:
    """Owner/admin-only production health snapshot for the operations console."""
    settings: Settings = request.app.state.settings
    warn_depth = max(1, int(getattr(settings, "OPS_QUEUE_WARN_DEPTH", 100)))
    critical_depth = max(warn_depth, int(getattr(settings, "OPS_QUEUE_CRIT_DEPTH", 1000)))

    postgres = await _probe_postgres(request, session, principal.tenant_id)
    redis, streams = await _probe_redis(request)

    deepest = max(streams, key=lambda stream: stream.length, default=None)
    max_backlog = deepest.length if deepest is not None else 0
    failed_events = postgres.dead_letter_events + postgres.raw_webhook_status_counts.get(
        "dead_letter", 0
    )
    status, alerts = compute_ops_status(
        postgres_ok=postgres.status == "ok",
        redis_ok=redis.status == "ok",
        max_backlog=max_backlog,
        failed_events=failed_events,
        warn_depth=warn_depth,
        critical_depth=critical_depth,
        backlog_source=deepest.stream if deepest is not None else None,
    )
    return OpsHealthRead(
        status=status,
        checked_at=datetime.now(UTC),
        queue_warn_depth=warn_depth,
        queue_critical_depth=critical_depth,
        max_backlog=max_backlog,
        alerts=alerts,
        postgres=postgres,
        redis=redis,
        streams=streams,
    )
