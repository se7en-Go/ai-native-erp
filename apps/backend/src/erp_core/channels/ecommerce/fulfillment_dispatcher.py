"""Storefront order fulfillment / tracking writeback dispatcher."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.listing_dispatcher import (
    CREDENTIAL_TYPES,
    build_marketplace_adapter,
    load_channel_credentials,
)
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)


def _extract_fulfillment_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AppError):
        return exc.code, (exc.detail or exc.title)[:500]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP_{exc.response.status_code}", str(exc)[:500]
    return type(exc).__name__, str(exc)[:500]


def _extract_external_fulfillment_id(result: Mapping[str, Any]) -> str | None:
    fulfillment = result.get("fulfillment")
    if isinstance(fulfillment, Mapping) and fulfillment.get("id"):
        return str(fulfillment["id"])
    for key in ("fulfillment_id", "external_fulfillment_id", "package_number", "id"):
        val = result.get(key)
        if val:
            return str(val)
    return None


async def push_order_fulfillment(
    session: AsyncSession,
    tenant_id: UUID,
    order_id: UUID,
    *,
    tracking_number: str,
    carrier_company: str,
    tracking_url: str | None = None,
    fulfillment_order_id: str | None = None,
    notify_customer: bool = True,
    raise_on_error: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Push tracking info back to the originating storefront/marketplace when applicable.

    Returns ``None`` for direct/manual orders without a linked channel account or external
    order id. On storefront errors, records failure metadata in ``orders.channel_metadata``
    and either returns a ``status='failed'`` summary (when ``raise_on_error=False``) or
    re-raises the typed error.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT o.id, o.channel_account_id, o.external_order_id, ca.channel_type
                FROM orders o
                LEFT JOIN channel_accounts ca
                  ON ca.id = o.channel_account_id AND ca.tenant_id = o.tenant_id
                WHERE o.tenant_id = :tid AND o.id = :oid
                """
            ),
            {"tid": tenant_id, "oid": order_id},
        )
    ).one_or_none()
    if row is None:
        raise AppError(
            slug="order-not-found",
            status_code=404,
            code="ERR_ORDER_NOT_FOUND",
            title="Order Not Found",
            detail=f"Order '{order_id}' not found.",
        )

    if row.channel_account_id is None or not row.external_order_id or not row.channel_type:
        return None

    channel_type = str(row.channel_type).strip().lower()
    cred_type = CREDENTIAL_TYPES.get(channel_type)
    if cred_type is None:
        return None

    vault = CredentialVaultHelper()
    try:
        credentials, expires_at = await load_channel_credentials(
            vault,
            session,
            tenant_id,
            row.channel_account_id,
            channel_type,
            cred_type,
        )
        adapter = build_marketplace_adapter(
            channel_type,
            credentials,
            transport=transport,
            expires_at=expires_at,
            tenant_id=tenant_id,
            channel_account_id=row.channel_account_id,
            vault=vault,
            session=session,
        )
        if adapter is None:
            return None
        if http_client is not None and hasattr(adapter, "_http_client"):
            adapter._http_client = http_client

        raw = await adapter.create_fulfillment(
            external_order_id=str(row.external_order_id),
            tracking_number=tracking_number,
            carrier_company=carrier_company,
            tracking_url=tracking_url,
            fulfillment_order_id=fulfillment_order_id,
            notify_customer=notify_customer,
        )
    except (AppError, httpx.HTTPError) as exc:
        err_code, err_detail = _extract_fulfillment_error(exc)
        failed_meta = {
            "status": "failed",
            "channel": channel_type,
            "tracking_number": tracking_number,
            "carrier_company": carrier_company,
            "error_code": err_code,
            "error_detail": err_detail,
            "failed_at": datetime.now(UTC).isoformat(),
        }
        await session.execute(
            text(
                """
                UPDATE orders
                SET channel_metadata = COALESCE(channel_metadata, '{}'::jsonb)
                        || CAST(:patch AS jsonb),
                    updated_at = now()
                WHERE tenant_id = :tid AND id = :oid
                """
            ),
            {
                "tid": tenant_id,
                "oid": order_id,
                "patch": json.dumps({"external_fulfillment": failed_meta}),
            },
        )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id=f"fulfillment:{channel_type}",
            action="ecommerce.fulfillment_push_failed",
            resource_type="order",
            resource_id=order_id,
            after_state=failed_meta,
        )
        logger.warning(
            "ecommerce_fulfillment_push_failed",
            tenant_id=str(tenant_id),
            order_id=str(order_id),
            channel_type=channel_type,
            error_code=err_code,
        )
        if raise_on_error:
            if isinstance(exc, AppError):
                raise
            raise AppError(
                slug="fulfillment-writeback-failed",
                status_code=502,
                code="ERR_FULFILLMENT_WRITEBACK_FAILED",
                title="Fulfillment Writeback Failed",
                detail=err_detail,
            ) from exc
        return failed_meta

    ext_fulfillment_id = _extract_external_fulfillment_id(raw)
    synced_meta = {
        "status": "synced",
        "channel": channel_type,
        "tracking_number": tracking_number,
        "carrier_company": carrier_company,
        "external_fulfillment_id": ext_fulfillment_id,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    await session.execute(
        text(
            """
            UPDATE orders
            SET channel_metadata = COALESCE(channel_metadata, '{}'::jsonb) || CAST(:patch AS jsonb),
                updated_at = now()
            WHERE tenant_id = :tid AND id = :oid
            """
        ),
        {
            "tid": tenant_id,
            "oid": order_id,
            "patch": json.dumps({"external_fulfillment": synced_meta}),
        },
    )
    await record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_type="system_cron",
        actor_id=f"fulfillment:{channel_type}",
        action="ecommerce.fulfillment_pushed",
        resource_type="order",
        resource_id=order_id,
        after_state=synced_meta,
    )
    return {**synced_meta, "raw": raw}
