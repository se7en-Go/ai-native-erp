from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.schemas import AuditEventRead

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TENANT_CHECK = (
    "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
)


def _normalize_ip(ip_address: str | None) -> str | None:
    if not ip_address:
        return None
    try:
        return str(ipaddress.ip_address(ip_address.strip()))
    except ValueError:
        return None


def _quote_ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe SQL identifier: {name}")
    return f'"{name}"'


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + offset
    return index // 12, index % 12 + 1


async def _ensure_rolling_monthly_partitions(session: AsyncSession) -> None:
    """Create the current UTC month and the next three if they are missing.

    New partitions are created in the same transaction that enables their RLS.
    """
    now = datetime.now(UTC)
    for offset in range(4):
        year, month = _shift_month(now.year, now.month, offset)
        next_year, next_month = _shift_month(now.year, now.month, offset + 1)
        name = f"audit_events_{year:04d}_{month:02d}"
        start = datetime(year, month, 1, tzinfo=UTC).isoformat()
        end = datetime(next_year, next_month, 1, tzinfo=UTC).isoformat()
        await session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {_quote_ident(name)} "
                "PARTITION OF audit_events "
                f"FOR VALUES FROM ('{start}') TO ('{end}')"
            )
        )


async def ensure_audit_child_rls(session: AsyncSession) -> list[str]:
    """Create the managed monthly partitions, then FORCE RLS on every child.

    0001 creates partitions without child relrowsecurity. This runs as the table
    owner (app_migrator) during application startup and whenever partitions are
    managed. app_runtime is never granted DDL. A later direct GRANT on a child
    still cannot read another tenant.
    """
    await _ensure_rolling_monthly_partitions(session)
    rows = (
        await session.execute(
            text(
                "SELECT c.relname "
                "FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'public.audit_events'::regclass "
                "ORDER BY c.relname"
            )
        )
    ).all()
    names = [str(row.relname) for row in rows]
    for name in names:
        quoted = _quote_ident(name)
        await session.execute(text(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY"))
        await session.execute(text(f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY"))
        for suffix, kind in (
            ("isolation_permissive", "PERMISSIVE"),
            ("isolation_restrictive", "RESTRICTIVE"),
        ):
            policy = f"{name}_{suffix}"
            _quote_ident(policy)
            exists = await session.scalar(
                text(
                    "SELECT 1 FROM pg_policy "
                    "WHERE polname = :policy AND polrelid = to_regclass(:rel)"
                ),
                {"policy": policy, "rel": f"public.{name}"},
            )
            if exists:
                continue
            await session.execute(
                text(
                    f"CREATE POLICY {_quote_ident(policy)} ON {quoted} "
                    f"AS {kind} FOR ALL TO app_runtime "
                    f"USING ({_TENANT_CHECK}) "
                    f"WITH CHECK ({_TENANT_CHECK})"
                )
            )
    return names


async def record_audit_event(
    session: AsyncSession,
    tenant_id: UUID,
    actor_type: Literal["human_user", "ai_agent", "system_cron", "webhook"],
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: UUID,
    *,
    proposal_id: UUID | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    normalized_ip = _normalize_ip(ip_address)
    event_id = await session.scalar(
        text(
            "INSERT INTO audit_events "
            "(tenant_id, actor_type, actor_id, action, resource_type, resource_id, "
            " proposal_id, before_state, after_state, ip_address, user_agent) "
            "VALUES (:tid, :actor_type, :actor_id, :action, :resource_type, :resource_id, "
            "        :proposal_id, CAST(:before_state AS jsonb), CAST(:after_state AS jsonb), "
            "        CAST(:ip_address AS inet), :user_agent) "
            "RETURNING id"
        ),
        {
            "tid": str(tenant_id),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "proposal_id": str(proposal_id) if proposal_id else None,
            "before_state": (
                json.dumps(before_state, default=str) if before_state is not None else None
            ),
            "after_state": (
                json.dumps(after_state, default=str) if after_state is not None else None
            ),
            "ip_address": normalized_ip,
            "user_agent": user_agent,
        },
    )
    return int(event_id or 0)


async def list_audit_events(
    session: AsyncSession,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> PaginatedResponse[AuditEventRead]:
    where_clauses: list[str] = ["1 = 1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if action:
        where_clauses.append("action = :action")
        params["action"] = action
    if resource_type:
        where_clauses.append("resource_type = :resource_type")
        params["resource_type"] = resource_type

    where_sql = " AND ".join(where_clauses)
    total = int(
        await session.scalar(
            text(f"SELECT count(*) FROM audit_events WHERE {where_sql}"),
            params,
        )
        or 0
    )
    rows = (
        await session.execute(
            text(
                "SELECT id, tenant_id, created_at, actor_type, actor_id, action, "
                "resource_type, resource_id, proposal_id, before_state, after_state, "
                "host(ip_address) AS ip_address, user_agent "
                f"FROM audit_events WHERE {where_sql} "
                "ORDER BY created_at DESC, id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).all()
    items = [
        AuditEventRead(
            id=int(row.id),
            tenant_id=UUID(str(row.tenant_id)),
            created_at=row.created_at,
            actor_type=str(row.actor_type),
            actor_id=str(row.actor_id),
            action=str(row.action),
            resource_type=str(row.resource_type),
            resource_id=UUID(str(row.resource_id)),
            proposal_id=UUID(str(row.proposal_id)) if row.proposal_id else None,
            before_state=row.before_state,
            after_state=row.after_state,
            ip_address=str(row.ip_address) if row.ip_address else None,
            user_agent=str(row.user_agent) if row.user_agent else None,
        )
        for row in rows
    ]
    return PaginatedResponse.build(items=items, total=total, limit=limit, offset=offset)
