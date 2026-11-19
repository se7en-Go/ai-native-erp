from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse


async def page_of[T, R](
    session: AsyncSession,
    query: sa.Select[T],
    serialize: Callable[[T], R],
    limit: int,
    offset: int,
) -> PaginatedResponse[R]:
    total = await session.scalar(
        sa.select(sa.func.count()).select_from(query.order_by(None).subquery())
    )
    rows = await session.scalars(query.limit(limit).offset(offset))
    return PaginatedResponse.build(
        items=[serialize(row) for row in rows], total=total or 0, limit=limit, offset=offset
    )


def record_not_found(entity: str) -> AppError:
    return AppError(
        slug="record-not-found",
        title="Record Not Found",
        status_code=404,
        detail=f"{entity} not found in this workspace.",
        code="ERR_RECORD_NOT_FOUND",
    )


def conflicting_change(detail: str) -> AppError:
    return AppError(
        slug="state-conflict",
        title="State Conflict",
        status_code=409,
        detail=detail,
        code="ERR_STATE_CONFLICT",
    )


def escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
