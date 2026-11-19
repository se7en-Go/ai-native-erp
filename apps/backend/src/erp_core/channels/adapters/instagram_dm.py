from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, SecretStr

from erp_core.channels.adapters.meta import (
    MetaMessagingAdapter,
    _nonempty_string,
    _object,
    _objects,
    validate_graph_account,
)
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


def _story_fields(payload: dict[str, Any]) -> dict[str, str]:
    return {key: value for key in ("id", "url") if (value := _nonempty_string(payload.get(key)))}


class InstagramDMAdapter(MetaMessagingAdapter):
    channel_types = (ChannelType.INSTAGRAM_DM,)
    identity_type = IdentityType.IG_PSID
    object_type = "instagram"
    token_key = "access_token"
    expired_code = "ERR_INSTAGRAM_WINDOW_EXPIRED"

    def _send_path(self, external_account_id: str) -> str:
        return f"/{external_account_id}/messages"

    def _metadata(self, message: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        reply_to = _object(message.get("reply_to"))
        if story := _story_fields(_object(reply_to.get("story"))):
            meta["story_reply"] = story
        if mid := _nonempty_string(reply_to.get("mid")):
            meta["reply_to_message_id"] = mid
        for attachment in _objects(message.get("attachments")):
            if attachment.get("type") == "story_mention" and (
                mention := _story_fields(_object(attachment.get("payload")))
            ):
                meta["story_mention"] = mention
                break
        return meta


class InstagramConnectRequest(BaseModel):
    ig_account_id: str = Field(pattern=r"^[0-9]{1,32}$")
    access_token: SecretStr
    display_name: str = Field(min_length=1, max_length=150)


instagram_router = APIRouter(
    prefix="/api/v1/channels/instagram",
    tags=["channels"],
    route_class=MessagingConnectRoute,
)


@instagram_router.post("/connect")
async def connect_instagram(
    payload: InstagramConnectRequest,
    request: Request,
    principal: ChannelAdmin,
) -> ConnectMessagingResponse:
    token = provider_token(payload.access_token)
    account = payload.ig_account_id
    await validate_graph_account(f"/{account}", account, token, provider_transport(request))
    tenant = principal.tenant_id
    async with tenant_session(messaging_sessions(request), tenant) as session:
        aid = await register_messaging_account(
            session,
            tenant,
            ChannelType.INSTAGRAM_DM,
            account,
            payload.display_name,
        )
        await save_messaging_credentials(
            session, tenant, aid, {InstagramDMAdapter.token_key: token}
        )
    return ConnectMessagingResponse(channel_account_id=aid, external_account_id=account)
