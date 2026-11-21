"""HTTP surface for grounded AI auto-reply and AI customer profiling.

Every route is tenant-scoped through :func:`get_tenant_db` (RLS session variable) and
enforces the AGENT mailbox isolation rule inherited from the grounded proposal generator.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.auto_reply_service import (
    AutoReplyRunRequest,
    AutoReplyRunResponse,
    AutoReplyService,
    AutoReplySettingRead,
    AutoReplySettingUpdate,
    CustomerAiProfileRead,
    ProfileExtractionRequest,
)
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai-autoreply"])

Principal = Annotated[CurrentPrincipal, Depends(get_current_principal)]
InboxRole = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)),
]
Db = Annotated[AsyncSession, Depends(get_tenant_db)]


def _service(request: Request) -> AutoReplyService:
    """Factory seam so tests can inject a deterministic offline service."""
    service = getattr(request.app.state, "auto_reply_service", None)
    if isinstance(service, AutoReplyService):
        return service
    return AutoReplyService()


Service = Annotated[AutoReplyService, Depends(_service)]


def _client_context(request: Request) -> tuple[str | None, str | None]:
    ip_address = request.client.host if request.client is not None else None
    return ip_address, request.headers.get("user-agent")


@router.get("/conversations/{conversation_id}/auto-reply")
async def get_auto_reply_setting(
    conversation_id: UUID,
    session: Db,
    principal: InboxRole,
    service: Service,
) -> AutoReplySettingRead:
    """Read the per-conversation AI auto-reply toggle."""
    return await service.get_setting(session, principal, conversation_id)


@router.patch("/conversations/{conversation_id}/auto-reply")
async def update_auto_reply_setting(
    conversation_id: UUID,
    body: AutoReplySettingUpdate,
    request: Request,
    session: Db,
    principal: InboxRole,
    service: Service,
) -> AutoReplySettingRead:
    """Toggle AI auto-reply for one conversation and write an audit event."""
    ip_address, user_agent = _client_context(request)
    return await service.update_setting(
        session,
        principal,
        conversation_id,
        body.enabled,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.post("/conversations/{conversation_id}/auto-reply/run")
async def run_auto_reply(
    conversation_id: UUID,
    body: AutoReplyRunRequest,
    request: Request,
    session: Db,
    principal: InboxRole,
    service: Service,
) -> AutoReplyRunResponse:
    """Draft or send one grounded reply; risk-sensitive threads are forced to L1."""
    ip_address, user_agent = _client_context(request)
    return await service.run(
        session,
        principal,
        conversation_id,
        body,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.post("/conversations/{conversation_id}/extract-profile")
async def extract_customer_profile(
    conversation_id: UUID,
    body: ProfileExtractionRequest,
    request: Request,
    session: Db,
    principal: InboxRole,
    service: Service,
) -> CustomerAiProfileRead:
    """Extract the structured AI customer profile and optionally sync it to the CRM."""
    ip_address, user_agent = _client_context(request)
    return await service.extract_profile(
        session,
        principal,
        conversation_id,
        sync_to_crm=body.sync_to_crm,
        ip_address=ip_address,
        user_agent=user_agent,
    )


@router.get("/customers/{customer_id}/profile")
async def get_customer_ai_profile(
    customer_id: UUID,
    session: Db,
    principal: Principal,
    service: Service,
) -> CustomerAiProfileRead:
    """Return the stored AI profile, or an empty-but-valid structure when unprofiled."""
    return await service.read_profile(session, principal, customer_id)


__all__ = ["router"]
