"""Transactional outbox dispatcher and reconciliation for 16-platform inventory sync.

Every supported channel (Shopify, WooCommerce, Shopline, Shoplazza, TikTok Shop,
Amazon SP-API, eBay, Alibaba.com, Shopee, Lazada, Temu, SHEIN, AliExpress, Ozon, Walmart
and Mercado Libre) is pushed through the single
:func:`~erp_core.channels.ecommerce.listing_dispatcher.build_marketplace_adapter`
factory, so a channel registered there is dispatched here with no second registry.

The push contract is uniform: every adapter implements
``push_inventory_level(external_sku=..., quantity=..., ...)`` with the same keyword
set, so the optional ``location_id``/``warehouse_id`` location hints are always passed
and each adapter uses the ones its API needs (TikTok Shop / Ozon warehouses, Shopify /
WooCommerce locations) and ignores the rest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.listing_dispatcher import (
    CREDENTIAL_TYPES,
    build_marketplace_adapter,
    load_channel_credentials,
)
from erp_core.orders.inventory_service import compute_sellable_channel_qty
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
    tenant_session,
)
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

MAX_SYNC_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600)


class InventorySyncJobRead(BaseModel):
    """Serialized status row for one cross-channel inventory sync job."""

    id: UUID
    tenant_id: UUID
    variant_id: UUID
    mapping_id: UUID
    channel_account_id: UUID
    channel_type: str
    external_sku: str
    requested_qty: int
    synced_qty: int | None = None
    status: str
    attempts: int
    next_retry_at: datetime
    error_code: str | None = None
    error_detail: str | None = None
    trigger_source: str
    created_at: datetime
    updated_at: datetime
    channel_label: str | None = None
    internal_sku: str | None = None


class InventorySyncDispatchSummary(BaseModel):
    """Counters + job details returned by manual or cron inventory dispatches."""

    enqueued: int = 0
    dispatched: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_letter: int = 0
    jobs: list[InventorySyncJobRead] = Field(default_factory=list)


def _row_to_job_read(row: Any) -> InventorySyncJobRead:
    return InventorySyncJobRead(
        id=row.id,
        tenant_id=row.tenant_id,
        variant_id=row.variant_id,
        mapping_id=row.mapping_id,
        channel_account_id=row.channel_account_id,
        channel_type=str(row.channel_type),
        external_sku=str(row.external_sku),
        requested_qty=int(row.requested_qty),
        synced_qty=int(row.synced_qty) if row.synced_qty is not None else None,
        status=str(row.status),
        attempts=int(row.attempts),
        next_retry_at=row.next_retry_at,
        error_code=row.error_code,
        error_detail=row.error_detail,
        trigger_source=str(row.trigger_source),
        created_at=row.created_at,
        updated_at=row.updated_at,
        channel_label=getattr(row, "channel_label", None),
        internal_sku=getattr(row, "internal_sku", None),
    )


def _config_text(config: Mapping[str, Any], *keys: str) -> str | None:
    """First non-empty string among ``keys`` in a channel account's ``config`` JSON."""
    for key in keys:
        value = config.get(key)
        if value in (None, ""):
            continue
        rendered = str(value).strip()
        if rendered:
            return rendered
    return None


def _extract_sync_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AppError):
        return exc.code, (exc.detail or exc.title)[:500]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP_{exc.response.status_code}", str(exc)[:500]
    return type(exc).__name__, str(exc)[:500]


class InventorySyncDispatcher:
    """Dispatches transactional ``inventory_sync_jobs`` across all 16 marketplaces."""

    def __init__(
        self,
        *,
        credential_vault: CredentialVaultHelper | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._vault = credential_vault or CredentialVaultHelper()
        self._transport = transport

    async def _push_one(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        row: Any,
    ) -> dict[str, Any]:
        channel_type = str(row.channel_type).strip().lower()
        cred_type = CREDENTIAL_TYPES.get(channel_type)
        if cred_type is None:
            raise AppError(
                slug="channel-inventory-sync-unsupported",
                status_code=422,
                code="ERR_CHANNEL_INVENTORY_SYNC_UNSUPPORTED",
                title="Channel Inventory Sync Unsupported",
                detail=f"Channel type '{channel_type}' does not support live inventory push.",
            )
        credentials, expires_at = await load_channel_credentials(
            self._vault,
            session,
            tenant_id,
            row.channel_account_id,
            channel_type,
            cred_type,
        )
        adapter = build_marketplace_adapter(
            channel_type,
            credentials,
            transport=self._transport,
            expires_at=expires_at,
            tenant_id=tenant_id,
            channel_account_id=row.channel_account_id,
            vault=self._vault,
            session=session,
        )
        if adapter is None or not hasattr(adapter, "push_inventory_level"):
            raise AppError(
                slug="channel-inventory-sync-unsupported",
                status_code=422,
                code="ERR_CHANNEL_INVENTORY_SYNC_UNSUPPORTED",
                title="Channel Inventory Sync Unsupported",
                detail=f"Channel '{channel_type}' adapter has no inventory push implementation.",
            )
        account_config: Mapping[str, Any] = row.account_config or {}
        # Location hints are optional and channel-specific: ``location_id`` is what the
        # Shopify/WooCommerce-style location APIs need, while ``warehouse_id`` is what
        # TikTok Shop, Ozon and the other warehouse-based marketplaces need. Each adapter
        # takes the full keyword set and ignores whatever its own API does not model.
        # Only marketplace-side ids are read here; ``channel_accounts.config.warehouse_id``
        # is the *ERP* warehouse UUID and must never be sent to a marketplace.
        location_id = _config_text(account_config, "location_id", "marketplace_location_id")
        warehouse_id = _config_text(
            account_config,
            "tiktok_warehouse_id",
            "ozon_warehouse_id",
            "marketplace_warehouse_id",
            "warehouse_external_id",
        )
        result: dict[str, Any] = await adapter.push_inventory_level(
            external_sku=str(row.external_sku),
            quantity=int(row.requested_qty),
            external_product_id=row.external_product_id,
            external_variant_id=row.external_variant_id,
            external_inventory_item_id=row.external_inventory_item_id,
            location_id=location_id,
            warehouse_id=warehouse_id,
        )
        return result

    async def dispatch_pending_jobs(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        limit: int = 50,
        job_ids: Sequence[UUID] | None = None,
    ) -> InventorySyncDispatchSummary:
        """Process due sync jobs inside the caller's tenant session with SKIP LOCKED."""
        if job_ids is not None:
            if not job_ids:
                return InventorySyncDispatchSummary()
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT j.*,
                               m.external_product_id,
                               m.external_variant_id,
                               m.external_inventory_item_id,
                               ca.config AS account_config,
                               ca.display_name AS channel_label,
                               pv.sku AS internal_sku
                        FROM inventory_sync_jobs j
                        JOIN platform_sku_mappings m
                          ON m.id = j.mapping_id AND m.tenant_id = j.tenant_id
                        JOIN channel_accounts ca
                          ON ca.id = j.channel_account_id AND ca.tenant_id = j.tenant_id
                        LEFT JOIN product_variants pv
                          ON pv.id = j.variant_id AND pv.tenant_id = j.tenant_id
                        WHERE j.tenant_id = :tid
                          AND j.id = ANY(:job_ids)
                        ORDER BY j.created_at ASC
                        FOR UPDATE OF j SKIP LOCKED
                        """
                    ),
                    {"tid": tenant_id, "job_ids": list(job_ids)},
                )
            ).all()
        else:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT j.*,
                               m.external_product_id,
                               m.external_variant_id,
                               m.external_inventory_item_id,
                               ca.config AS account_config,
                               ca.display_name AS channel_label,
                               pv.sku AS internal_sku
                        FROM inventory_sync_jobs j
                        JOIN platform_sku_mappings m
                          ON m.id = j.mapping_id AND m.tenant_id = j.tenant_id
                        JOIN channel_accounts ca
                          ON ca.id = j.channel_account_id AND ca.tenant_id = j.tenant_id
                        LEFT JOIN product_variants pv
                          ON pv.id = j.variant_id AND pv.tenant_id = j.tenant_id
                        WHERE j.tenant_id = :tid
                          AND j.status IN ('pending', 'failed')
                          AND j.next_retry_at <= now()
                        ORDER BY j.next_retry_at ASC, j.created_at ASC
                        LIMIT :lim
                        FOR UPDATE OF j SKIP LOCKED
                        """
                    ),
                    {"tid": tenant_id, "lim": limit},
                )
            ).all()

        dispatched_ids: list[UUID] = []
        succeeded = 0
        failed = 0
        dead_letter = 0

        for row in rows:
            dispatched_ids.append(row.id)
            now = datetime.now(UTC)
            try:
                push_result = await self._push_one(session, tenant_id, row)
            except Exception as exc:
                new_attempts = int(row.attempts) + 1
                err_code, err_detail = _extract_sync_error(exc)
                non_retriable = err_code == "ERR_CHANNEL_INVENTORY_SYNC_UNSUPPORTED"
                if non_retriable or new_attempts >= MAX_SYNC_ATTEMPTS:
                    new_status = "dead_letter"
                    next_retry = now
                    dead_letter += 1
                else:
                    delay_sec = _RETRY_BACKOFF_SECONDS[
                        min(new_attempts - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    new_status = "failed"
                    next_retry = now + timedelta(seconds=delay_sec)
                    failed += 1

                await session.execute(
                    text(
                        """
                        UPDATE inventory_sync_jobs
                        SET status = :status,
                            attempts = :attempts,
                            next_retry_at = :next_retry,
                            error_code = :err_code,
                            error_detail = :err_detail,
                            updated_at = now()
                        WHERE tenant_id = :tid AND id = :jid
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "jid": row.id,
                        "status": new_status,
                        "attempts": new_attempts,
                        "next_retry": next_retry,
                        "err_code": err_code,
                        "err_detail": err_detail,
                    },
                )
                await session.execute(
                    text(
                        """
                        UPDATE platform_sku_mappings
                        SET last_sync_status = :status,
                            last_sync_error = :err_detail
                        WHERE tenant_id = :tid AND id = :mid
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "mid": row.mapping_id,
                        "status": new_status,
                        "err_detail": err_detail,
                    },
                )
                logger.warning(
                    "inventory_sync_job_failed",
                    tenant_id=str(tenant_id),
                    job_id=str(row.id),
                    channel_type=str(row.channel_type),
                    external_sku=str(row.external_sku),
                    status=new_status,
                    attempts=new_attempts,
                    error_code=err_code,
                )
                continue

            ext_inv_id = (
                str(push_result.get("external_inventory_item_id") or "").strip() or None
            )
            synced_qty = int(row.requested_qty)
            succeeded += 1
            await session.execute(
                text(
                    """
                    UPDATE inventory_sync_jobs
                    SET status = 'succeeded',
                        synced_qty = :qty,
                        attempts = attempts + 1,
                        error_code = NULL,
                        error_detail = NULL,
                        updated_at = now()
                    WHERE tenant_id = :tid AND id = :jid
                    """
                ),
                {"tid": tenant_id, "jid": row.id, "qty": synced_qty},
            )
            await session.execute(
                text(
                    """
                    UPDATE platform_sku_mappings
                    SET last_inventory_synced_qty = :qty,
                        last_inventory_synced_at = now(),
                        last_sync_status = 'succeeded',
                        last_sync_error = NULL,
                        external_inventory_item_id = COALESCE(
                            :ext_item_id, external_inventory_item_id
                        )
                    WHERE tenant_id = :tid AND id = :mid
                    """
                ),
                {
                    "tid": tenant_id,
                    "mid": row.mapping_id,
                    "qty": synced_qty,
                    "ext_item_id": ext_inv_id,
                },
            )
            await record_audit_event(
                session,
                tenant_id=tenant_id,
                actor_type="system_cron" if row.trigger_source == "cron" else "human_user",
                actor_id="channels.inventory_sync_dispatcher",
                action="inventory.channel_synced",
                resource_type="platform_sku_mapping",
                resource_id=row.mapping_id,
                after_state={
                    "channel_type": str(row.channel_type),
                    "external_sku": str(row.external_sku),
                    "synced_qty": synced_qty,
                    "trigger_source": str(row.trigger_source),
                },
            )

        jobs = await self._load_jobs_by_ids(session, tenant_id, dispatched_ids)
        return InventorySyncDispatchSummary(
            enqueued=len(dispatched_ids),
            dispatched=len(dispatched_ids),
            succeeded=succeeded,
            failed=failed,
            dead_letter=dead_letter,
            jobs=jobs,
        )

    @staticmethod
    async def _load_jobs_by_ids(
        session: AsyncSession, tenant_id: UUID, job_ids: Sequence[UUID]
    ) -> list[InventorySyncJobRead]:
        if not job_ids:
            return []
        rows = (
            await session.execute(
                text(
                    """
                    SELECT j.*,
                           ca.display_name AS channel_label,
                           pv.sku AS internal_sku
                    FROM inventory_sync_jobs j
                    LEFT JOIN channel_accounts ca
                      ON ca.id = j.channel_account_id AND ca.tenant_id = j.tenant_id
                    LEFT JOIN product_variants pv
                      ON pv.id = j.variant_id AND pv.tenant_id = j.tenant_id
                    WHERE j.tenant_id = :tid AND j.id = ANY(:job_ids)
                    ORDER BY j.created_at DESC, j.id DESC
                    """
                ),
                {"tid": tenant_id, "job_ids": list(job_ids)},
            )
        ).all()
        return [_row_to_job_read(r) for r in rows]

    async def enqueue_matching_mappings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        variant_id: UUID | None = None,
        channel_account_id: UUID | None = None,
        trigger_source: str = "manual",
        only_drifted: bool = False,
    ) -> list[UUID]:
        """Insert or coalesce pending sync jobs for matching active SKU mappings."""
        filters = [
            "psm.tenant_id = :tid",
            "psm.sync_inventory_enabled = true",
            "ca.status = 'active'",
        ]
        params: dict[str, Any] = {"tid": tenant_id}
        if variant_id is not None:
            filters.append("psm.variant_id = :vid")
            params["vid"] = variant_id
        if channel_account_id is not None:
            filters.append("psm.channel_account_id = :caid")
            params["caid"] = channel_account_id

        where_sql = " AND ".join(filters)
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT psm.id AS mapping_id,
                           psm.variant_id,
                           psm.channel_account_id,
                           ca.channel_type,
                           psm.external_sku,
                           psm.safety_stock,
                           psm.bundle_multiplier,
                           psm.last_inventory_synced_qty,
                           COALESCE(bal.available_qty, 0) AS available_qty
                    FROM platform_sku_mappings psm
                    JOIN channel_accounts ca
                      ON ca.id = psm.channel_account_id AND ca.tenant_id = psm.tenant_id
                    LEFT JOIN (
                        SELECT tenant_id, variant_id,
                               GREATEST(
                                   0, COALESCE(SUM(on_hand_qty - reserved_qty), 0)
                               ) AS available_qty
                        FROM inventory_balances
                        WHERE tenant_id = :tid
                        GROUP BY tenant_id, variant_id
                    ) bal ON bal.tenant_id = psm.tenant_id AND bal.variant_id = psm.variant_id
                    WHERE {where_sql}
                    ORDER BY psm.created_at ASC
                    """
                ),
                params,
            )
        ).all()

        queued_ids: list[UUID] = []
        for row in rows:
            sellable_qty = compute_sellable_channel_qty(
                int(row.available_qty or 0),
                safety_stock=int(row.safety_stock or 0),
                bundle_multiplier=int(row.bundle_multiplier or 1),
            )
            if only_drifted and row.last_inventory_synced_qty == sellable_qty:
                continue
            job_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO inventory_sync_jobs (
                            tenant_id, variant_id, mapping_id, channel_account_id,
                            channel_type, external_sku, requested_qty, status,
                            attempts, next_retry_at, trigger_source
                        ) VALUES (
                            :tid, :vid, :mid, :caid,
                            :ctype, :ext_sku, :qty, 'pending',
                            0, now(), :tsrc
                        )
                        ON CONFLICT (tenant_id, mapping_id) WHERE status = 'pending'
                        DO UPDATE SET
                            requested_qty = EXCLUDED.requested_qty,
                            external_sku = EXCLUDED.external_sku,
                            channel_type = EXCLUDED.channel_type,
                            trigger_source = EXCLUDED.trigger_source,
                            next_retry_at = now(),
                            updated_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "tid": tenant_id,
                        "vid": row.variant_id,
                        "mid": row.mapping_id,
                        "caid": row.channel_account_id,
                        "ctype": str(row.channel_type),
                        "ext_sku": str(row.external_sku),
                        "qty": sellable_qty,
                        "tsrc": trigger_source,
                    },
                )
            ).scalar_one()
            queued_ids.append(UUID(str(job_id)))
        return queued_ids

    async def sync_inventory_now(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        variant_id: UUID | None = None,
        channel_account_id: UUID | None = None,
        trigger_source: str = "manual",
    ) -> InventorySyncDispatchSummary:
        """Immediately enqueue and push current sellable inventory across matching channels."""
        queued_ids = await self.enqueue_matching_mappings(
            session,
            tenant_id,
            variant_id=variant_id,
            channel_account_id=channel_account_id,
            trigger_source=trigger_source,
        )
        if not queued_ids:
            return InventorySyncDispatchSummary()
        summary = await self.dispatch_pending_jobs(session, tenant_id, job_ids=queued_ids)
        summary.enqueued = len(queued_ids)
        return summary

    async def retry_sync_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        job_id: UUID,
    ) -> InventorySyncJobRead:
        """Reset one failed/dead-letter sync job with fresh stock and dispatch it."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT j.id, j.variant_id, j.mapping_id,
                           psm.safety_stock, psm.bundle_multiplier
                    FROM inventory_sync_jobs j
                    JOIN platform_sku_mappings psm
                      ON psm.id = j.mapping_id AND psm.tenant_id = j.tenant_id
                    WHERE j.tenant_id = :tid AND j.id = :jid
                    FOR UPDATE OF j
                    """
                ),
                {"tid": tenant_id, "jid": job_id},
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="sync-job-not-found",
                status_code=404,
                code="ERR_SYNC_JOB_NOT_FOUND",
                title="Sync Job Not Found",
                detail=f"Inventory sync job '{job_id}' was not found.",
            )

        available_qty = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT GREATEST(0, COALESCE(SUM(on_hand_qty - reserved_qty), 0))
                        FROM inventory_balances
                        WHERE tenant_id = :tid AND variant_id = :vid
                        """
                    ),
                    {"tid": tenant_id, "vid": row.variant_id},
                )
            ).scalar_one()
        )
        fresh_qty = compute_sellable_channel_qty(
            available_qty,
            safety_stock=int(row.safety_stock or 0),
            bundle_multiplier=int(row.bundle_multiplier or 1),
        )
        # Transition through ``processing`` so a concurrent pending row never collides
        # with the partial unique index ``uq_inv_sync_jobs_pending_mapping``.
        await session.execute(
            text(
                """
                UPDATE inventory_sync_jobs
                SET status = 'processing',
                    requested_qty = :qty,
                    attempts = 0,
                    next_retry_at = now(),
                    error_code = NULL,
                    error_detail = NULL,
                    trigger_source = 'manual_retry',
                    updated_at = now()
                WHERE tenant_id = :tid AND id = :jid
                """
            ),
            {"tid": tenant_id, "jid": job_id, "qty": fresh_qty},
        )
        await self.dispatch_pending_jobs(session, tenant_id, job_ids=[job_id])
        jobs = await self._load_jobs_by_ids(session, tenant_id, [job_id])
        if not jobs:  # pragma: no cover - defensive
            raise AppError(
                slug="sync-job-not-found",
                status_code=404,
                code="ERR_SYNC_JOB_NOT_FOUND",
                title="Sync Job Not Found",
                detail=f"Inventory sync job '{job_id}' could not be reloaded.",
            )
        return jobs[0]

    async def list_sync_jobs(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        variant_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[InventorySyncJobRead], int]:
        filters = ["j.tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": limit, "off": offset}
        if variant_id is not None:
            filters.append("j.variant_id = :vid")
            params["vid"] = variant_id
        if status is not None and status.strip():
            filters.append("j.status = :status")
            params["status"] = status.strip()
        where_sql = " AND ".join(filters)
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM inventory_sync_jobs j WHERE {where_sql}"),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT j.*,
                           ca.display_name AS channel_label,
                           pv.sku AS internal_sku
                    FROM inventory_sync_jobs j
                    LEFT JOIN channel_accounts ca
                      ON ca.id = j.channel_account_id AND ca.tenant_id = j.tenant_id
                    LEFT JOIN product_variants pv
                      ON pv.id = j.variant_id AND pv.tenant_id = j.tenant_id
                    WHERE {where_sql}
                    ORDER BY j.created_at DESC, j.id DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).all()
        return [_row_to_job_read(r) for r in rows], total


async def drain_inventory_sync_all_tenants(
    broker_session_factory: async_sessionmaker[AsyncSession],
    runtime_session_factory: async_sessionmaker[AsyncSession],
    *,
    limit_per_tenant: int = 50,
    dispatcher: InventorySyncDispatcher | None = None,
    tenant_ids: Sequence[UUID] | None = None,
) -> dict[str, int]:
    """Drain due inventory outbox jobs across active (or specified) tenants."""
    if tenant_ids is not None:
        resolved_tids = [UUID(str(t)) for t in tenant_ids]
    else:
        async with system_session(broker_session_factory) as sys_session:
            raw_rows = (
                await sys_session.execute(
                    text(
                        """
                        SELECT DISTINCT j.tenant_id
                        FROM inventory_sync_jobs j
                        JOIN tenants t ON t.id = j.tenant_id
                        WHERE t.is_active
                          AND j.status IN ('pending', 'failed')
                          AND j.next_retry_at <= now()
                        """
                    )
                )
            ).scalars().all()
        resolved_tids = [UUID(str(t)) for t in raw_rows]

    syncer = dispatcher or InventorySyncDispatcher()
    totals = {"tenants": 0, "dispatched": 0, "succeeded": 0, "failed": 0, "dead_letter": 0}
    for tid in resolved_tids:
        totals["tenants"] += 1
        try:
            async with tenant_session(runtime_session_factory, tid) as session:
                summary = await syncer.dispatch_pending_jobs(
                    session, tid, limit=limit_per_tenant
                )
                totals["dispatched"] += summary.dispatched
                totals["succeeded"] += summary.succeeded
                totals["failed"] += summary.failed
                totals["dead_letter"] += summary.dead_letter
        except Exception:
            logger.exception("inventory_sync_tenant_drain_failed", tenant_id=str(tid))
    return totals


async def reconcile_inventory_sync_cron(
    *,
    broker_session_factory: async_sessionmaker[AsyncSession] | None = None,
    runtime_session_factory: async_sessionmaker[AsyncSession] | None = None,
    dispatcher: InventorySyncDispatcher | None = None,
    tenant_ids: Sequence[UUID] | None = None,
) -> dict[str, int]:
    """30-minute drift sweep: detect mismatched channel quantities and push corrections."""
    owned_engines: list[Any] = []
    b_factory = broker_session_factory
    r_factory = runtime_session_factory
    if b_factory is None or r_factory is None:
        settings = get_settings()
        if b_factory is None:
            m_engine = create_migrator_engine(settings)
            owned_engines.append(m_engine)
            b_factory = create_session_factory(m_engine)
        if r_factory is None:
            rt_engine = create_runtime_engine(settings)
            owned_engines.append(rt_engine)
            r_factory = create_session_factory(rt_engine)

    syncer = dispatcher or InventorySyncDispatcher()
    totals = {"tenants": 0, "enqueued": 0, "succeeded": 0, "failed": 0, "dead_letter": 0}
    try:
        if tenant_ids is not None:
            resolved_tids = [UUID(str(t)) for t in tenant_ids]
        else:
            async with system_session(b_factory) as sys_session:
                raw_rows = (
                    await sys_session.execute(
                        text("SELECT id FROM tenants WHERE is_active ORDER BY created_at ASC")
                    )
                ).scalars().all()
            resolved_tids = [UUID(str(t)) for t in raw_rows]

        for tid in resolved_tids:
            totals["tenants"] += 1
            try:
                async with tenant_session(r_factory, tid) as session:
                    queued_ids = await syncer.enqueue_matching_mappings(
                        session,
                        tid,
                        trigger_source="cron",
                        only_drifted=True,
                    )
                    totals["enqueued"] += len(queued_ids)
                    if queued_ids:
                        summary = await syncer.dispatch_pending_jobs(
                            session, tid, job_ids=queued_ids
                        )
                        totals["succeeded"] += summary.succeeded
                        totals["failed"] += summary.failed
                        totals["dead_letter"] += summary.dead_letter
            except Exception:
                logger.exception("inventory_sync_cron_tenant_failed", tenant_id=str(tid))
        return totals
    finally:
        for eng in owned_engines:
            await eng.dispose()


__all__ = [
    "InventorySyncDispatchSummary",
    "InventorySyncDispatcher",
    "InventorySyncJobRead",
    "MAX_SYNC_ATTEMPTS",
    "drain_inventory_sync_all_tenants",
    "reconcile_inventory_sync_cron",
]
