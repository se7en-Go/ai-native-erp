"""Minute-by-minute dispatch of due `scheduled` social posts through the live APIs.

The social module never imports a commerce domain (`infra/importlinter.ini` contract
`social_isolated`), so this cron owns the cross-tenant sweep and hands each tenant a
fail-closed RLS session:

1. the directory (migrator) session lists active tenants — it runs with
   `app.current_tenant_id` cleared, so it cannot read tenant rows;
2. each tenant is visited inside `tenant_session(...)`, which sets
   `app.current_tenant_id` for the transaction, and
   :meth:`SocialService.dispatch_due_scheduled_posts` locks the due rows with
   `FOR UPDATE SKIP LOCKED` before calling the vendor API.

A failure for one tenant is logged and skipped so one broken workspace cannot stop the
sweep. A failed post is persisted as `failed` (with the sanitized upstream code in
`metrics.publish_error`), which is what keeps the next sweep from retrying it forever.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
    tenant_session,
)
from erp_core.social.publishers import SocialPublisherPort
from erp_core.social.service import SocialService

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]


async def dispatch_due_social_posts_cron(
    *,
    broker_session_factory: Factory | None = None,
    runtime_session_factory: Factory | None = None,
    publisher: SocialPublisherPort | None = None,
    now: datetime | None = None,
    tenant_ids: Sequence[UUID] | None = None,
) -> dict[str, int]:
    """Publish every due scheduled post for every active tenant.

    Engines/factories are injectable so the integration suite drives this against a real
    PostgreSQL 17 pair (`app_migrator` directory + `app_runtime` tenant sessions) without
    a second set of connections. A tenant whose sweep raises is rolled back as a whole and
    logged; the remaining tenants still run.

    Returns ``{"tenants", "scanned", "published", "failed"}``, where `tenants` counts the
    active tenants that actually had a due post.
    """
    settings = get_settings()
    directory_engine = None
    runtime_engine = None
    if broker_session_factory is None:
        directory_engine = create_migrator_engine(settings)
        broker_session_factory = create_session_factory(directory_engine)
    if runtime_session_factory is None:
        runtime_engine = create_runtime_engine(settings)
        runtime_session_factory = create_session_factory(runtime_engine)

    totals = {"tenants": 0, "scanned": 0, "published": 0, "failed": 0}
    moment = now or datetime.now(UTC)
    service = SocialService(EnvelopeCryptoService(settings), publisher=publisher)
    try:
        async with system_session(broker_session_factory) as directory:
            if tenant_ids is not None:
                active_ids: list[UUID] = [
                    row.id
                    for row in (
                        await directory.execute(
                            text(
                                "SELECT id FROM tenants "
                                "WHERE is_active AND id = ANY(CAST(:ids AS uuid[]))"
                            ),
                            {"ids": [str(tid) for tid in tenant_ids]},
                        )
                    ).all()
                ]
            else:
                active_ids = [
                    row.id
                    for row in (
                        await directory.execute(text("SELECT id FROM tenants WHERE is_active"))
                    ).all()
                ]
        for tenant_id in active_ids:
            try:
                async with tenant_session(runtime_session_factory, tenant_id) as session:
                    stats = await service.dispatch_due_scheduled_posts(
                        session, tenant_id, now=moment
                    )
            except Exception:
                # Rolled back as a whole; one broken tenant must not stop the sweep.
                logger.exception("social dispatch failed tenant=%s", tenant_id)
                continue
            if stats["scanned"] == 0:
                continue
            totals["tenants"] += 1
            for key in ("scanned", "published", "failed"):
                totals[key] += stats[key]
        if totals["scanned"]:
            logger.info("social dispatch sweep: %s", totals)
        return totals
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if directory_engine is not None:
            await directory_engine.dispose()
