from __future__ import annotations

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
    save_messaging_credentials,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, IdentityType


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
        aid = await register_messaging_account(
            session,
            tenant,
            ChannelType.MESSENGER,
            payload.page_id,
            payload.display_name,
        )
        await save_messaging_credentials(session, tenant, aid, {MessengerAdapter.token_key: token})
    return ConnectMessagingResponse(channel_account_id=aid, external_account_id=payload.page_id)
