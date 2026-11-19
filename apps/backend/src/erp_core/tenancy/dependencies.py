from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.security import decode_token


class CurrentPrincipal(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role: UserRole
    email: str


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AppError(
            slug="missing-authorization",
            title="Authorization Header Missing",
            status_code=401,
            detail="Bearer token is required",
            code="ERR_MISSING_TOKEN",
        )
    parts = authorization.split(" ", maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AppError(
            slug="invalid-authorization-header",
            title="Invalid Authorization Header",
            status_code=401,
            detail="Authorization header must use Bearer scheme",
            code="ERR_INVALID_TOKEN",
        )
    return parts[1].strip()


async def get_current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentPrincipal:
    settings: Settings = request.app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    token = _extract_bearer_token(authorization)
    payload = decode_token(token, settings, expected_type="access")

    async with tenant_session(session_factory, payload.tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT m.role, u.email "
                    "FROM tenant_memberships m "
                    "JOIN tenants t ON t.id = m.tenant_id "
                    "JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :tid "
                    "  AND m.user_id = :uid "
                    "  AND m.is_active = true "
                    "  AND u.is_active = true "
                    "  AND t.is_active = true "
                    "  AND t.shredded_at IS NULL"
                ),
                {"tid": str(payload.tid), "uid": str(payload.sub)},
            )
        ).first()

    if row is None:
        raise AppError(
            slug="membership-inactive",
            title="Inactive Tenant Membership",
            status_code=401,
            detail="User membership or tenant workspace is inactive or revoked",
            code="ERR_INVALID_TOKEN",
        )

    return CurrentPrincipal(
        user_id=payload.sub,
        tenant_id=payload.tid,
        role=UserRole(str(row.role)),
        email=str(row.email),
    )


def require_roles(*allowed_roles: UserRole) -> Callable[..., Awaitable[CurrentPrincipal]]:
    allowed = frozenset(allowed_roles)

    async def _role_guard(
        principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    ) -> CurrentPrincipal:
        if allowed and principal.role not in allowed:
            raise AppError(
                slug="insufficient-role",
                title="Insufficient Role Permissions",
                status_code=403,
                detail=f"Role '{principal.role.value}' is not authorized for this operation",
                code="ERR_INSUFFICIENT_ROLE",
            )
        return principal

    return _role_guard


async def get_tenant_db(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with tenant_session(session_factory, principal.tenant_id) as session:
        yield session
