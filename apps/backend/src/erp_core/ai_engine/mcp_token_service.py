"""Scoped MCP bearer tokens: creation, listing, revocation and pre-tenant auth.

Every external MCP request arrives with no tenant context, so authentication goes
through `resolve_mcp_token(TEXT)` — a `SECURITY DEFINER` helper whose RESTRICTIVE
`app_migrator` policy is pinned to the exact SHA-256 hash of the presented token.
Only the hash and a short prefix are stored; the plaintext token leaves the server
exactly once, in the `POST /api/v1/ai/mcp/tokens` response.
"""

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

from erp_core.ai_engine.mcp_tools import (
    MCP_SCOPE_READ,
    MCP_TOOL_NAMES,
    MCP_VALID_SCOPES,
)
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

TOKEN_PREFIX_MARKER = "erp_mcp_"
DEFAULT_MCP_SCOPES: tuple[str, ...] = (MCP_SCOPE_READ,)

_TOKEN_PREFIX_LENGTH = len(TOKEN_PREFIX_MARKER) + 8


def hash_mcp_token(raw_token: str) -> str:
    """Return the lowercase SHA-256 hex digest of a plain MCP bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def token_display_prefix(raw_token: str) -> str:
    return raw_token[:_TOKEN_PREFIX_LENGTH]


def _invalid_token_error(detail: str = "MCP API token is invalid or revoked.") -> AppError:
    return AppError(
        slug="invalid-mcp-token",
        title="Invalid MCP Token",
        status_code=401,
        detail=detail,
        code="ERR_INVALID_MCP_TOKEN",
    )


def _validation_error(detail: str) -> AppError:
    return AppError(
        slug="invalid-mcp-token-request",
        title="Invalid MCP Token Request",
        status_code=422,
        detail=detail,
        code="ERR_VALIDATION",
    )


class McpTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_MCP_SCOPES))
    allowed_tools: list[str] | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class McpTokenRead(BaseModel):
    id: UUID
    name: str
    token_prefix: str
    scopes: list[str]
    allowed_tools: list[str] | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class McpTokenCreateResponse(BaseModel):
    token: McpTokenRead
    plain_token: str


class ResolvedMcpToken(BaseModel):
    token_id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    scopes: list[str]
    allowed_tools: list[str] | None = None

    def actor_id(self) -> str:
        return f"mcp:{self.token_id}"


def _row_to_read(row: Mapping[Any, Any]) -> McpTokenRead:
    allowed = row["allowed_tools"]
    return McpTokenRead(
        id=row["id"],
        name=str(row["name"]),
        token_prefix=str(row["token_prefix"]),
        scopes=[str(scope) for scope in (row["scopes"] or [])],
        allowed_tools=[str(tool) for tool in allowed] if allowed is not None else None,
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
    )


def extract_bearer_token(authorization_or_header: str | None) -> str | None:
    """Accept a raw `erp_mcp_...` token, a `Bearer ...` header, or nothing."""
    if not authorization_or_header:
        return None
    candidate = authorization_or_header.strip()
    if not candidate:
        return None
    parts = candidate.split(" ", maxsplit=1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        candidate = parts[1].strip()
    return candidate or None


def normalise_scopes(raw_scopes: list[str] | None) -> list[str]:
    """Validate and de-duplicate requested scopes, keeping catalog order."""
    requested = {str(scope).strip() for scope in raw_scopes or [] if str(scope).strip()}
    unknown = sorted(requested - MCP_VALID_SCOPES)
    if unknown:
        raise _validation_error(
            "Unknown MCP scope(s): "
            + ", ".join(unknown)
            + ". Valid scopes are: "
            + ", ".join(sorted(MCP_VALID_SCOPES))
            + "."
        )
    if not requested:
        raise _validation_error("At least one MCP scope is required.")
    return sorted(requested)


def normalise_allowed_tools(raw_tools: list[str] | None) -> list[str] | None:
    """Validate the optional per-token tool whitelist against the live catalog."""
    if raw_tools is None:
        return None
    cleaned = sorted({str(tool).strip() for tool in raw_tools if str(tool).strip()})
    if not cleaned:
        return None
    unknown = sorted(set(cleaned) - MCP_TOOL_NAMES)
    if unknown:
        raise _validation_error(
            "Unknown MCP tool(s): "
            + ", ".join(unknown)
            + ". Available tools are: "
            + ", ".join(sorted(MCP_TOOL_NAMES))
            + "."
        )
    return cleaned


class McpTokenService:
    """Manage tenant MCP tokens and resolve them before any tenant context exists."""

    async def create_token(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        created_by_user_id: UUID,
        req: McpTokenCreateRequest,
    ) -> McpTokenCreateResponse:
        clean_name = req.name.strip()
        if not clean_name:
            raise _validation_error("Token name must not be blank.")
        scopes = normalise_scopes(req.scopes)
        allowed_tools = normalise_allowed_tools(req.allowed_tools)
        token_id = uuid4()
        plain_token = f"{TOKEN_PREFIX_MARKER}{secrets.token_urlsafe(32)}"
        token_prefix = token_display_prefix(plain_token)
        token_hash = hash_mcp_token(plain_token)
        expires_at = (
            datetime.now(UTC) + timedelta(days=req.expires_in_days)
            if req.expires_in_days is not None
            else None
        )
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO mcp_api_tokens (
                        id, tenant_id, created_by_user_id, name,
                        token_prefix, token_hash, scopes, allowed_tools, expires_at
                    ) VALUES (
                        :id, :tid, :uid, :name, :prefix, :thash,
                        CAST(:scopes AS TEXT[]), CAST(:allowed AS TEXT[]), :exp
                    )
                    RETURNING id, name, token_prefix, scopes, allowed_tools,
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
                    "allowed": allowed_tools,
                    "exp": expires_at,
                },
            )
        ).one()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(created_by_user_id),
            action="ai.mcp_token.created",
            resource_type="mcp_api_token",
            resource_id=token_id,
            after_state={
                "name": clean_name,
                "token_prefix": token_prefix,
                "scopes": scopes,
                "allowed_tools": allowed_tools,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return McpTokenCreateResponse(
            token=_row_to_read(row._mapping),
            plain_token=plain_token,
        )

    async def list_tokens(self, session: AsyncSession, tenant_id: UUID) -> list[McpTokenRead]:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, token_prefix, scopes, allowed_tools,
                           last_used_at, expires_at, revoked_at, created_at
                    FROM mcp_api_tokens
                    WHERE tenant_id = :tid
                    ORDER BY created_at DESC, id
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
    ) -> McpTokenRead:
        row = (
            await session.execute(
                text(
                    """
                    UPDATE mcp_api_tokens
                    SET revoked_at = COALESCE(revoked_at, now())
                    WHERE id = :id AND tenant_id = :tid
                    RETURNING id, name, token_prefix, scopes, allowed_tools,
                              last_used_at, expires_at, revoked_at, created_at
                    """
                ),
                {"id": token_id, "tid": tenant_id},
            )
        ).first()
        if row is None:
            raise AppError(
                slug="mcp-token-not-found",
                title="MCP Token Not Found",
                status_code=404,
                detail=f"MCP token {token_id} was not found.",
                code="ERR_MCP_TOKEN_NOT_FOUND",
            )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="ai.mcp_token.revoked",
            resource_type="mcp_api_token",
            resource_id=token_id,
            after_state={"revoked": True},
        )
        return _row_to_read(row._mapping)

    async def authenticate_token(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        authorization_or_header: str | None,
        *,
        required_scope: str | None = None,
    ) -> ResolvedMcpToken:
        """Resolve a presented token to its tenant, scopes and tool whitelist."""
        candidate = extract_bearer_token(authorization_or_header)
        if candidate is None or not candidate.startswith(TOKEN_PREFIX_MARKER):
            raise _invalid_token_error()
        token_hash = hash_mcp_token(candidate)
        async with system_session(session_factory) as sys_session:
            row = (
                await sys_session.execute(
                    text(
                        """
                        SELECT token_id, tenant_id, created_by_user_id,
                               scopes, allowed_tools, expires_at, revoked_at
                        FROM resolve_mcp_token(:token_hash)
                        """
                    ),
                    {"token_hash": token_hash},
                )
            ).first()
        if row is None:
            raise _invalid_token_error()
        if row.revoked_at is not None:
            raise _invalid_token_error("MCP API token has been revoked.")
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            raise _invalid_token_error("MCP API token has expired.")
        scopes = [str(scope) for scope in (row.scopes or [])]
        if required_scope is not None and required_scope not in scopes:
            raise _invalid_token_error("MCP API token lacks the required scope.")
        allowed = row.allowed_tools
        allowed_tools = [str(tool) for tool in allowed] if allowed is not None else None

        async with tenant_session(session_factory, row.tenant_id) as t_session:
            await t_session.execute(
                text(
                    """
                    UPDATE mcp_api_tokens
                    SET last_used_at = now()
                    WHERE id = :id AND tenant_id = :tid
                    """
                ),
                {"id": row.token_id, "tid": row.tenant_id},
            )
        return ResolvedMcpToken(
            token_id=row.token_id,
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            scopes=scopes,
            allowed_tools=allowed_tools,
        )
