"""Narrow SECURITY DEFINER access to raw webhook rows.

Direct table reads stay tenant-scoped. Ingest, claim, and status changes go
through validated functions so unresolved payloads are not listed by runtime.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def accept_raw_webhook_event(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    channel_type: str,
    external_event_id: str,
    external_account_id: str | None,
    headers_json: str,
    payload_json: str,
) -> uuid.UUID | None:
    inserted = (
        await session.execute(
            text(
                "SELECT accept_raw_webhook_event("
                "CAST(:id AS uuid), CAST(:tid AS uuid), :ctype, :eeid, :eaid, "
                "CAST(:headers AS jsonb), CAST(:payload AS jsonb))"
            ),
            {
                "id": event_id,
                "tid": tenant_id,
                "ctype": channel_type,
                "eeid": external_event_id,
                "eaid": external_account_id,
                "headers": headers_json,
                "payload": payload_json,
            },
        )
    ).scalar_one_or_none()
    if inserted is None:
        return None
    return uuid.UUID(str(inserted))


async def claim_raw_webhook_event(
    session: AsyncSession, raw_event_id: uuid.UUID
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM claim_raw_webhook_event(CAST(:id AS uuid))"),
                {"id": raw_event_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return dict(row)


async def set_raw_webhook_processing_state(
    session: AsyncSession,
    *,
    raw_event_id: uuid.UUID,
    status: str,
    tenant_id: uuid.UUID | None = None,
    attempt: int | None = None,
    error_message: str | None = None,
) -> None:
    await session.execute(
        text(
            "SELECT set_raw_webhook_processing_state("
            "CAST(:id AS uuid), :status, CAST(:tid AS uuid), :attempt, :err)"
        ),
        {
            "id": raw_event_id,
            "status": status,
            "tid": tenant_id,
            "attempt": attempt,
            "err": error_message,
        },
    )


async def list_unenqueued_raw_webhook_ids(
    session: AsyncSession, limit: int = 20
) -> list[tuple[uuid.UUID, str]]:
    rows = (
        await session.execute(
            text("SELECT id, channel_type FROM list_unenqueued_raw_webhook_ids(:lim)"),
            {"lim": limit},
        )
    ).all()
    return [(uuid.UUID(str(row[0])), str(row[1])) for row in rows]
