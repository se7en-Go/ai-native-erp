"""Tenant-scoped connection and outbound state shared by messaging providers."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, SecretStr
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    register_channel_routing,
    resolve_routing_tenant,
)
from erp_core.shared.enums import ChannelType, ChannelVisibilityScope, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

MESSAGING_CREDENTIAL = "messaging_provider"
ChannelAdmin = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
ChannelConnectorPrincipal = Annotated[
    CurrentPrincipal,
    Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
]


class ConnectMessagingResponse(BaseModel):
    channel_account_id: UUID
    external_account_id: str
    display_name: str | None = None
    owner_user_id: UUID | None = None
    visibility_scope: Literal["shared", "personal"] = "shared"
    bot_username: str | None = None


def invalid_connection(detail: str) -> AppError:
    return AppError(
        slug="invalid-channel-connection",
        title="Invalid Channel Connection",
        status_code=422,
        code="ERR_INVALID_CHANNEL_CONNECTION",
        detail=detail,
    )


class MessagingConnectRoute(APIRoute):
    """Validation errors must never reflect credential input back to logs or clients."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise invalid_connection("Check the channel connection fields.") from None

        return handle


def provider_token(secret: SecretStr) -> str:
    token = secret.get_secret_value()
    if not 1 <= len(token) <= 2048 or any(not 33 <= ord(c) <= 126 for c in token):
        raise invalid_connection("The provider token is invalid.")
    return token


def provider_transport(request: Request) -> httpx.AsyncBaseTransport | None:
    return cast(
        httpx.AsyncBaseTransport | None,
        getattr(request.app.state, "messaging_http_transport", None),
    )


def messaging_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


def _routing_conflict() -> AppError:
    return AppError(
        slug="channel-routing-conflict",
        title="Channel Already Connected",
        status_code=409,
        code="ERR_CHANNEL_ROUTING_CONFLICT",
        detail="This provider account is already connected to a different workspace.",
    )


def _insufficient_role(detail: str) -> AppError:
    return AppError(
        slug="insufficient-role",
        title="Insufficient Role",
        status_code=403,
        code="ERR_INSUFFICIENT_ROLE",
        detail=detail,
    )


async def is_active_tenant_member(
    session: AsyncSession, tenant_id: UUID, user_id: UUID | None
) -> bool:
    """True only for an active membership row inside this workspace."""
    if user_id is None:
        return False
    return bool(
        await session.scalar(
            text(
                "SELECT 1 FROM tenant_memberships "
                "WHERE tenant_id = :tid AND user_id = :uid AND is_active = true"
            ),
            {"tid": tenant_id, "uid": user_id},
        )
    )


async def resolve_connect_ownership(
    session: AsyncSession,
    principal: CurrentPrincipal,
    requested_scope: Literal["shared", "personal"] | None,
    requested_owner_user_id: UUID | None,
    *,
    allow_agent_self_bind: bool,
) -> tuple[UUID | None, Literal["shared", "personal"]]:
    """Decide the owner/scope of a channel account being connected right now.

    Admins may bind company (shared) channels or hand a personal channel to any active
    member. Sales roles may only ever bind a personal channel owned by themselves, and
    only for channel types that support self-service binding.
    """
    if principal.role in (UserRole.OWNER, UserRole.ADMIN):
        if requested_scope == ChannelVisibilityScope.PERSONAL.value or (
            requested_owner_user_id is not None
        ):
            owner_id = requested_owner_user_id or principal.user_id
            if not await is_active_tenant_member(session, principal.tenant_id, owner_id):
                raise invalid_connection(
                    "Selected owner is not an active member of this workspace."
                )
            return owner_id, "personal"
        return None, "shared"

    if not allow_agent_self_bind:
        raise _insufficient_role("Only workspace admins can bind this channel.")
    if requested_scope == ChannelVisibilityScope.SHARED.value or (
        requested_owner_user_id is not None and requested_owner_user_id != principal.user_id
    ):
        raise _insufficient_role(
            "Sales agents can only bind personal channels owned by themselves."
        )
    return principal.user_id, "personal"


async def assert_channel_bind_allowed(
    session: AsyncSession,
    tenant_id: UUID,
    channel: ChannelType,
    external_id: str,
    *,
    actor_user_id: UUID | None,
    actor_is_admin: bool,
) -> None:
    """Anti-hijack guard: a sales role can never re-bind a company or peer-owned account."""
    if actor_is_admin:
        return
    existing = (
        (
            await session.execute(
                text(
                    "SELECT id, owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND channel_type = :channel "
                    "AND external_account_id = :external"
                ),
                {
                    "tid": tenant_id,
                    "channel": channel.value,
                    "external": external_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None and existing["owner_user_id"] != actor_user_id:
        raise _routing_conflict()


async def register_messaging_account(
    session: AsyncSession,
    tenant_id: UUID,
    channel: ChannelType,
    external_id: str,
    display_name: str,
    owner_user_id: UUID | None = None,
    visibility_scope: Literal["shared", "personal"] = "shared",
    actor_user_id: UUID | None = None,
    actor_is_admin: bool = True,
    config_patch: dict[str, Any] | None = None,
) -> UUID:
    await assert_channel_bind_allowed(
        session,
        tenant_id,
        channel,
        external_id,
        actor_user_id=actor_user_id,
        actor_is_admin=actor_is_admin,
    )
    aid = (
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, "
                "owner_user_id, visibility_scope, status, config) "
                "VALUES (:id, :tid, :channel, :external, :name, :owner, :scope, 'active', "
                "CAST(:cfg AS jsonb)) "
                "ON CONFLICT (tenant_id, channel_type, external_account_id) DO UPDATE "
                "SET display_name = EXCLUDED.display_name, status = 'active', "
                "owner_user_id = EXCLUDED.owner_user_id, "
                "visibility_scope = EXCLUDED.visibility_scope, "
                "config = COALESCE(channel_accounts.config, '{}'::jsonb) "
                "|| CAST(:cfg AS jsonb), updated_at = now() "
                "RETURNING id"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "channel": channel.value,
                "external": external_id,
                "name": display_name,
                "owner": owner_user_id,
                "scope": visibility_scope,
                "cfg": json.dumps(config_patch or {}),
            },
        )
    ).scalar_one()
    try:
        await register_channel_routing(
            session,
            channel_type=channel.value,
            external_account_id=external_id,
            tenant_id=tenant_id,
            channel_account_id=aid,
        )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise _routing_conflict() from None
        raise
    # With a concurrent first claim, 0003c can report success after a foreign-owner
    # ON CONFLICT no-op. Never treat its return string as proof of ownership.
    if await resolve_routing_tenant(session, channel.value, external_id) != (tenant_id, aid):
        raise _routing_conflict()
    return cast(UUID, aid)


async def save_messaging_credentials(
    session: AsyncSession,
    tenant_id: UUID,
    account_id: UUID,
    credentials: dict[str, Any],
) -> None:
    await CredentialVaultHelper().save_secret_json(
        session,
        tenant_id,
        account_id,
        MESSAGING_CREDENTIAL,
        credentials,
    )


async def load_outbound_context(
    session: AsyncSession,
    tenant_id: UUID,
    request: OutboundMessageRequest,
    channel: ChannelType,
) -> RowMapping | None:
    """Use tenant/account/conversation/message records, not caller-provided sender metadata."""
    if request.tenant_id != tenant_id or request.channel_type != channel:
        return None
    return (
        (
            await session.execute(
                text(
                    "SELECT c.external_thread_id, c.channel_policy_state, c.csw_expires_at, "
                    "c.human_agent_window_expires_at, m.sender_type, m.body_text, m.content_type "
                    "FROM messages m JOIN conversations c "
                    "ON c.tenant_id = m.tenant_id AND c.id = m.conversation_id "
                    "JOIN channel_accounts a "
                    "ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id "
                    "WHERE m.tenant_id = :tid AND m.id = :mid AND m.direction = 'outbound' "
                    "AND c.id = :cid AND c.channel_account_id = :aid "
                    "AND a.external_account_id = :external "
                    "AND a.status = 'active' AND a.channel_type = :channel "
                    "AND c.channel_type = :channel AND m.channel_type = :channel"
                ),
                {
                    "tid": tenant_id,
                    "mid": request.message_id,
                    "cid": request.conversation_id,
                    "aid": request.channel_account_id,
                    "external": request.external_account_id,
                    "channel": channel.value,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
