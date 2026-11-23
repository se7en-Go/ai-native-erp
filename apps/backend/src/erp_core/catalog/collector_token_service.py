"""Scoped API tokens for headless / browser-extension sourcing collection."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

DEFAULT_COLLECTOR_SCOPES: tuple[str, ...] = ("sourcing:collect",)
_TOKEN_PREFIX_MARKER = "erp_col_"


def hash_collector_token(raw_token: str) -> str:
    """Return lowercase SHA-256 hex digest of a plain collector API token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class CollectorTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class CollectorTokenRead(BaseModel):
    id: UUID
    name: str
    token_prefix: str
    scopes: list[str]
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class CollectorTokenCreateResponse(BaseModel):
    token: CollectorTokenRead
    plain_token: str


class ResolvedCollectorToken(BaseModel):
    token_id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    scopes: list[str]


def _invalid_token_error(detail: str = "Collector API token is invalid or revoked.") -> AppError:
    return AppError(
        slug="invalid-collector-token",
        title="Invalid Collector Token",
        status_code=401,
        detail=detail,
        code="ERR_INVALID_COLLECTOR_TOKEN",
    )


def _row_to_read(row: Mapping[Any, Any]) -> CollectorTokenRead:
    return CollectorTokenRead(
        id=row["id"],
        name=str(row["name"]),
        token_prefix=str(row["token_prefix"]),
        scopes=[str(s) for s in (row["scopes"] or [])],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
    )


class CollectorTokenService:
    """Manage tenant-scoped collector tokens and pre-tenant X-Collector-Token resolution."""

    async def create_token(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        created_by_user_id: UUID,
        req: CollectorTokenCreateRequest,
    ) -> CollectorTokenCreateResponse:
        clean_name = req.name.strip()
        if not clean_name:
            raise AppError(
                slug="collector-token-name-required",
                title="Collector Token Name Required",
                status_code=422,
                detail="Token name must not be blank.",
                code="ERR_VALIDATION",
            )
        token_id = uuid4()
        plain_token = f"{_TOKEN_PREFIX_MARKER}{secrets.token_urlsafe(32)}"
        token_prefix = plain_token[:16]
        token_hash = hash_collector_token(plain_token)
        scopes = list(DEFAULT_COLLECTOR_SCOPES)
        expires_at = (
            datetime.now(UTC) + timedelta(days=req.expires_in_days)
            if req.expires_in_days is not None
            else None
        )
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO collector_api_tokens (
                        id, tenant_id, created_by_user_id, name,
                        token_prefix, token_hash, scopes, expires_at
                    ) VALUES (
                        :id, :tid, :uid, :name, :prefix, :thash, CAST(:scopes AS TEXT[]), :exp
                    )
                    RETURNING id, name, token_prefix, scopes,
                              last_used_at, expires_at, revoked_at, created_at
                    """
                ),
                {
                    "id": token_id,
                    "tid": tenant_id,
                    "uid": created_by_user_id,
                    "name": clean_name,
                    "prefix": token_prefix,
                    "thash": token_hash,
                    "scopes": scopes,
                    "exp": expires_at,
                },
            )
        ).one()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(created_by_user_id),
            action="catalog.collector_token.created",
            resource_type="collector_api_token",
            resource_id=token_id,
            after_state={
                "name": clean_name,
                "token_prefix": token_prefix,
                "scopes": scopes,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return CollectorTokenCreateResponse(
            token=_row_to_read(row._mapping),
            plain_token=plain_token,
        )

    async def list_tokens(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[CollectorTokenRead]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, token_prefix, scopes,
                           last_used_at, expires_at, revoked_at, created_at
                    FROM collector_api_tokens
                    WHERE tenant_id = :tid
                    ORDER BY created_at DESC
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()
        return [_row_to_read(row._mapping) for row in rows]

    async def revoke_token(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        token_id: UUID,
        actor_id: str,
    ) -> CollectorTokenRead:
        row = (
            await session.execute(
                text(
                    """
                    UPDATE collector_api_tokens
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE id = :id AND tenant_id = :tid
                    RETURNING id, name, token_prefix, scopes,
                              last_used_at, expires_at, revoked_at, created_at
                    """
                ),
                {"id": token_id, "tid": tenant_id},
            )
        ).first()
        if row is None:
            raise AppError(
                slug="collector-token-not-found",
                title="Collector Token Not Found",
                status_code=404,
                detail=f"Collector token {token_id} was not found.",
                code="ERR_COLLECTOR_TOKEN_NOT_FOUND",
            )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.collector_token.revoked",
            resource_type="collector_api_token",
            resource_id=token_id,
            after_state={"revoked": True},
        )
        return _row_to_read(row._mapping)

    async def authenticate_token(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        raw_token: str | None,
        *,
        required_scope: str = "sourcing:collect",
    ) -> ResolvedCollectorToken:
        candidate = (raw_token or "").strip()
        if not candidate or not candidate.startswith(_TOKEN_PREFIX_MARKER):
            raise _invalid_token_error()
        token_hash = hash_collector_token(candidate)
        async with system_session(session_factory) as sys_session:
            row = (
                await sys_session.execute(
                    text(
                        """
                        SELECT token_id, tenant_id, created_by_user_id,
                               scopes, expires_at, revoked_at
                        FROM resolve_collector_token(:token_hash)
                        """
                    ),
                    {"token_hash": token_hash},
                )
            ).first()
        if row is None:
            raise _invalid_token_error()
        if row.revoked_at is not None:
            raise _invalid_token_error("Collector API token has been revoked.")
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            raise _invalid_token_error("Collector API token has expired.")
        scopes = [str(s) for s in (row.scopes or [])]
        if required_scope and required_scope not in scopes:
            raise _invalid_token_error("Collector API token lacks required scope.")

        async with tenant_session(session_factory, row.tenant_id) as t_session:
            await t_session.execute(
                text(
                    """
                    UPDATE collector_api_tokens
                    SET last_used_at = now()
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": row.token_id, "tid": row.tenant_id},
            )
        return ResolvedCollectorToken(
            token_id=row.token_id,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            scopes=scopes,
        )
