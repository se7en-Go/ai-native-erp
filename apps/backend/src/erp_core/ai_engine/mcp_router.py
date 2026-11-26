"""HTTP surface for the tenant-isolated MCP server.

Two prefixes live here:

* ``/api/v1/ai/mcp`` — operator console endpoints (token issuance/revocation and the
  exposed tool catalog) behind normal JWT + role RBAC. The web BFF already proxies
  everything under ``/api/v1/ai``.
* ``/api/v1/mcp`` — the external MCP Streamable HTTP endpoint authenticated with
  ``Authorization: Bearer erp_mcp_...`` or ``X-MCP-Token``. It accepts a single
  JSON-RPC 2.0 request or a batch and never needs a browser session.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.mcp_server import McpServer
from erp_core.ai_engine.mcp_token_service import (
    McpTokenCreateRequest,
    McpTokenCreateResponse,
    McpTokenRead,
    McpTokenService,
)
from erp_core.ai_engine.mcp_tools import (
    BLOCKED_L0_TOOLS,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    MCP_TOOL_SPECS,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/ai/mcp", tags=["ai-mcp"])
public_router = APIRouter(tags=["mcp"])

_TOKEN_READER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
_TOKEN_WRITER_ROLES = (UserRole.OWNER, UserRole.ADMIN)

TokenReader = Annotated[CurrentPrincipal, Depends(require_roles(*_TOKEN_READER_ROLES))]
TokenWriter = Annotated[CurrentPrincipal, Depends(require_roles(*_TOKEN_WRITER_ROLES))]


class McpExposedTool(BaseModel):
    name: str
    required_scope: str
    autonomy_level: str
    description: str
    input_schema: dict[str, Any]


class McpBlockedTool(BaseModel):
    name: str
    description: str
    reason: str


class McpToolCatalogResponse(BaseModel):
    protocol_version: str
    server: dict[str, str]
    tools: list[McpExposedTool]
    blocked_tools: list[McpBlockedTool]
    config_snippet: dict[str, Any]


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


def _token_service() -> McpTokenService:
    return McpTokenService()


def _mcp_server(request: Request) -> McpServer:
    server: McpServer | None = getattr(request.app.state, "mcp_server", None)
    if server is None:
        server = McpServer(_session_factory(request))
        request.app.state.mcp_server = server
    return server


@router.get("/tokens")
async def list_mcp_tokens(
    request: Request, principal: TokenReader
) -> list[McpTokenRead]:
    service = _token_service()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await service.list_tokens(session, principal.tenant_id)


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_mcp_token(
    request: Request, body: McpTokenCreateRequest, principal: TokenWriter
) -> McpTokenCreateResponse:
    service = _token_service()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await service.create_token(
            session, principal.tenant_id, principal.user_id, body
        )


@router.delete("/tokens/{token_id}")
async def revoke_mcp_token(
    token_id: UUID, request: Request, principal: TokenWriter
) -> McpTokenRead:
    service = _token_service()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await service.revoke_token(
            session, principal.tenant_id, token_id, str(principal.user_id)
        )


@router.get("/tools")
async def get_mcp_tool_catalog(
    request: Request, principal: TokenReader
) -> McpToolCatalogResponse:
    endpoint = f"{str(request.base_url).rstrip('/')}/api/v1/mcp"
    return McpToolCatalogResponse(
        protocol_version=MCP_PROTOCOL_VERSION,
        server={"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
        tools=[
            McpExposedTool(
                name=spec.name,
                required_scope=spec.required_scope,
                autonomy_level=spec.autonomy_level,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in MCP_TOOL_SPECS
        ],
        blocked_tools=[McpBlockedTool(**blocked) for blocked in BLOCKED_L0_TOOLS],
        config_snippet={
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "type": "http",
                    "url": endpoint,
                    "headers": {
                        "Authorization": "Bearer erp_mcp_<your-tenant-token>",
                    },
                }
            }
        },
    )


@public_router.get("/api/v1/mcp")
async def mcp_probe() -> Response:
    """Answer GET probes with an explicit 405 instead of crashing the client."""
    return Response(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        headers={
            "Allow": "POST",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-MCP-Server": f"{MCP_SERVER_NAME}/{MCP_SERVER_VERSION}",
        },
    )


@public_router.post("/api/v1/mcp")
async def mcp_endpoint(request: Request) -> Response:
    """MCP Streamable HTTP: one JSON-RPC request or a batch, tenant-scoped by token."""
    resolved = await _token_service().authenticate_token(
        _session_factory(request),
        request.headers.get("x-mcp-token") or request.headers.get("authorization"),
    )
    raw_body = await request.body()
    try:
        payload: Any = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
            headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        )

    result = await _mcp_server(request).handle(resolved, payload)
    if result is None:
        return Response(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result,
        headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
    )
