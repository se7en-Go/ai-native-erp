from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, SecretStr

from erp_core.channels.adapters.meta import MetaMessagingAdapter, validate_graph_account
from erp_core.channels.messaging_accounts import (
    ChannelAdmin,
    ConnectMessagingResponse,
    MessagingConnectRoute,
    messaging_sessions,
    provider_token,
    provider_transport,
    register_messaging_account,
    resolve_connect_ownership,
    save_messaging_credentials,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, IdentityType, UserRole


class MessengerAdapter(MetaMessagingAdapter):
    channel_types = (ChannelType.MESSENGER,)
    identity_type = IdentityType.FB_PSID
    object_type = "page"
    token_key = "page_access_token"
    expired_code = "ERR_MESSENGER_WINDOW_EXPIRED"


class MessengerConnectRequest(BaseModel):
    page_id: str = Field(pattern=r"^[0-9]{1,32}$")
    page_access_token: SecretStr
    display_name: str = Field(min_length=1, max_length=150)
    visibility_scope: Literal["shared", "personal"] | None = None
    owner_user_id: UUID | None = None


messenger_router = APIRouter(
    prefix="/api/v1/channels/messenger", tags=["channels"], route_class=MessagingConnectRoute
)


@messenger_router.post("/connect")
async def connect_messenger(
    payload: MessengerConnectRequest,
    request: Request,
    principal: ChannelAdmin,
) -> ConnectMessagingResponse:
    token = provider_token(payload.page_access_token)
    await validate_graph_account("/me", payload.page_id, token, provider_transport(request))
    tenant = principal.tenant_id
    async with tenant_session(messaging_sessions(request), tenant) as session:
        owner_uid, vis_scope = await resolve_connect_ownership(
            session,
            principal,
            payload.visibility_scope,
            payload.owner_user_id,
            allow_agent_self_bind=False,
        )
        aid = await register_messaging_account(
            session,
            tenant,
            ChannelType.MESSENGER,
            payload.page_id,
            payload.display_name,
            owner_user_id=owner_uid,
            visibility_scope=vis_scope,
            actor_user_id=principal.user_id,
            actor_is_admin=principal.role in (UserRole.OWNER, UserRole.ADMIN),
        )
        await save_messaging_credentials(session, tenant, aid, {MessengerAdapter.token_key: token})
    return ConnectMessagingResponse(
        channel_account_id=aid,
        external_account_id=payload.page_id,
        display_name=payload.display_name,
        owner_user_id=owner_uid,
        visibility_scope=vis_scope,
    )
