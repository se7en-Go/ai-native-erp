from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field, SecretStr
from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import register_channel_routing
from erp_core.channels.messaging_accounts import (
    ChannelConnectorPrincipal,
    ConnectMessagingResponse,
    MessagingConnectRoute,
    invalid_connection,
    is_active_tenant_member,
    messaging_sessions,
    register_messaging_account,
    resolve_connect_ownership,
    save_messaging_credentials,
)
from erp_core.channels.models import (
    ChannelAccount,
    ConsentRecord,
    DeadLetterEvent,
    SuppressionEntry,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelStatus, ChannelType, UserRole
from erp_core.shared.pagination import PaginatedResponse, PaginationParams
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])
email_router = APIRouter(route_class=MessagingConnectRoute)
Admin = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
InboxRole = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)),
]
Db = Annotated[AsyncSession, Depends(get_tenant_db)]
Page = Annotated[PaginationParams, Query()]


class ChannelRateLimitRead(BaseModel):
    mps: int | None = None
    coexistence: bool | None = None


class ChannelAccountRead(BaseModel):
    id: UUID
    channel_type: ChannelType
    external_account_id: str
    display_name: str
    status: ChannelStatus
    rate_limit_profile: ChannelRateLimitRead
    owner_user_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    visibility_scope: Literal["shared", "personal"] = "shared"
    bot_username: str | None = None


class UpdateChannelOwnershipRequest(BaseModel):
    visibility_scope: Literal["shared", "personal"]
    owner_user_id: UUID | None = None
    reassign_open_conversations: bool = False


class DisconnectChannelResponse(BaseModel):
    channel_account_id: UUID
    status: ChannelStatus = ChannelStatus.DISCONNECTED


class ChannelDlqRead(BaseModel):
    id: UUID
    channel_type: ChannelType
    external_event_id: str | None
    error_message: str
    retry_count: int
    created_at: datetime


class ChannelConsentRead(BaseModel):
    id: UUID
    customer_name: str
    channel_type: ChannelType
    consent_status: Literal["opted_in", "opted_out"]
    proof_source: str
    recorded_at: datetime


class ChannelSuppressionRead(BaseModel):
    id: UUID
    channel_type: ChannelType
    normalized_address: str
    reason: str
    created_at: datetime


class EmailConnectRequest(BaseModel):
    email_address: EmailStr
    display_name: str | None = Field(default=None, max_length=150)
    provider: Literal["resend", "smtp_imap"] = "resend"
    api_key: SecretStr | None = None
    smtp_host: str | None = Field(default=None, max_length=200)
    smtp_port: int | None = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, max_length=200)
    smtp_password: SecretStr | None = None
    imap_host: str | None = Field(default=None, max_length=200)
    imap_port: int | None = Field(default=993, ge=1, le=65535)
    visibility_scope: Literal["shared", "personal"] | None = None
    owner_user_id: UUID | None = None


class WebLivechatConnectRequest(BaseModel):
    site_domain: str = Field(min_length=3, max_length=255)
    display_name: str | None = Field(default=None, max_length=150)
    visibility_scope: Literal["shared", "personal"] | None = None
    owner_user_id: UUID | None = None


# Host/port characters only: the widget resolves its tenant from this normalized site key.
_SITE_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,250}$")


def normalize_site_domain(raw: str) -> str:
    """Reduce a pasted website URL to the host used as the LiveChat ``site_key``."""
    candidate = raw.strip().lower()
    for scheme in ("https://", "http://"):
        if candidate.startswith(scheme):
            candidate = candidate[len(scheme) :]
            break
    candidate = candidate.split("/", 1)[0]
    candidate = candidate.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not _SITE_DOMAIN_RE.fullmatch(candidate):
        raise invalid_connection("Enter a valid website domain.")
    return candidate


def _account_columns() -> list[Any]:
    """Channels never import the identity ORM; correlated SQL keeps ``config`` private."""
    owner_name: Any = literal_column(
        "(SELECT full_name FROM users WHERE users.id = channel_accounts.owner_user_id)"
    ).label("owner_name")
    owner_email: Any = literal_column(
        "(SELECT email FROM users WHERE users.id = channel_accounts.owner_user_id)"
    ).label("owner_email")
    bot_username: Any = literal_column(
        "channel_accounts.config->>'bot_username'"
    ).label("bot_username")
    return [
        ChannelAccount.id,
        ChannelAccount.channel_type,
        ChannelAccount.external_account_id,
        ChannelAccount.display_name,
        ChannelAccount.status,
        ChannelAccount.rate_limit_profile,
        ChannelAccount.owner_user_id,
        owner_name,
        owner_email,
        ChannelAccount.visibility_scope,
        bot_username,
    ]


async def _load_account(
    session: AsyncSession, tenant_id: UUID, account_id: UUID
) -> ChannelAccountRead:
    query = select(*_account_columns()).where(
        ChannelAccount.tenant_id == tenant_id, ChannelAccount.id == account_id
    )
    row = (await session.execute(query)).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return ChannelAccountRead.model_validate(row)


async def _page[T: BaseModel](
    session: AsyncSession,
    query: Any,
    page: PaginationParams,
    model: type[T],
) -> PaginatedResponse[T]:
    total = await session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = (await session.execute(query.limit(page.limit).offset(page.offset))).mappings()
    return PaginatedResponse[T].build(
        items=[model.model_validate(row) for row in rows],
        total=total or 0,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/accounts")
async def list_channel_accounts(
    session: Db,
    principal: InboxRole,
    page: Page,
    owner_user_id: Annotated[UUID | None, Query()] = None,
    visibility_scope: Annotated[Literal["shared", "personal"] | None, Query()] = None,
) -> PaginatedResponse[ChannelAccountRead]:
    table = ChannelAccount
    query = select(*_account_columns()).where(table.tenant_id == principal.tenant_id)
    if owner_user_id is not None:
        query = query.where(table.owner_user_id == owner_user_id)
    if visibility_scope is not None:
        query = query.where(table.visibility_scope == visibility_scope)
    if principal.role == UserRole.AGENT:
        # Sales agents only see their own personal channels and unowned company channels.
        query = query.where(
            or_(
                table.owner_user_id == principal.user_id,
                and_(
                    table.visibility_scope == "shared",
                    table.owner_user_id.is_(None),
                ),
            )
        )
    query = query.order_by(table.created_at.desc(), table.id)
    return await _page(session, query, page, ChannelAccountRead)


@router.patch("/accounts/{account_id}/ownership", response_model=ChannelAccountRead)
async def update_channel_ownership(
    account_id: UUID,
    payload: UpdateChannelOwnershipRequest,
    session: Db,
    principal: Admin,
) -> ChannelAccountRead:
    row = (
        (
            await session.execute(
                text(
                    "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": principal.tenant_id, "aid": account_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    if payload.visibility_scope == "personal":
        owner_id = payload.owner_user_id
        if owner_id is None:
            raise invalid_connection("A personal channel requires an owner.")
        if not await is_active_tenant_member(session, principal.tenant_id, owner_id):
            raise invalid_connection("Selected owner is not an active member of this workspace.")
        new_scope, new_owner = "personal", owner_id
    else:
        new_scope, new_owner = "shared", None

    await session.execute(
        text(
            "UPDATE channel_accounts SET owner_user_id = :owner, visibility_scope = :scope, "
            "updated_at = now() WHERE tenant_id = :tid AND id = :aid"
        ),
        {
            "owner": new_owner,
            "scope": new_scope,
            "tid": principal.tenant_id,
            "aid": account_id,
        },
    )
    if payload.reassign_open_conversations:
        await session.execute(
            text(
                "UPDATE conversations SET assigned_user_id = :owner, updated_at = now() "
                "WHERE tenant_id = :tid AND channel_account_id = :aid AND status != 'resolved'"
            ),
            {"owner": new_owner, "tid": principal.tenant_id, "aid": account_id},
        )
    await record_audit_event(
        session=session,
        tenant_id=principal.tenant_id,
        actor_type="human_user",
        actor_id=str(principal.user_id),
        action="channel.ownership_updated",
        resource_type="channel_account",
        resource_id=account_id,
        before_state={
            "owner_user_id": str(row["owner_user_id"]) if row["owner_user_id"] else None,
            "visibility_scope": row["visibility_scope"],
        },
        after_state={
            "owner_user_id": str(new_owner) if new_owner else None,
            "visibility_scope": new_scope,
            "reassign_open_conversations": payload.reassign_open_conversations,
        },
    )
    return await _load_account(session, principal.tenant_id, account_id)


@router.delete("/accounts/{account_id}", response_model=DisconnectChannelResponse)
async def disconnect_channel_account(
    account_id: UUID,
    session: Db,
    principal: ChannelConnectorPrincipal,
) -> DisconnectChannelResponse:
    row = (
        (
            await session.execute(
                text(
                    "SELECT channel_type, external_account_id, owner_user_id, visibility_scope "
                    "FROM channel_accounts WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": principal.tenant_id, "aid": account_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    is_admin = principal.role in (UserRole.OWNER, UserRole.ADMIN)
    owns_personal_channel = (
        row["visibility_scope"] == "personal" and row["owner_user_id"] == principal.user_id
    )
    if not is_admin and not owns_personal_channel:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="ERR_INSUFFICIENT_ROLE"
        )

    await session.execute(
        text(
            "UPDATE channel_accounts SET status = :st, updated_at = now() "
            "WHERE tenant_id = :tid AND id = :aid"
        ),
        {"st": ChannelStatus.DISCONNECTED.value, "tid": principal.tenant_id, "aid": account_id},
    )
    # app_runtime has no direct DELETE on the routing directory: deactivate through the
    # SECURITY DEFINER function instead of a raw DELETE that PostgreSQL would reject.
    await register_channel_routing(
        session,
        channel_type=str(row["channel_type"]),
        external_account_id=str(row["external_account_id"]),
        tenant_id=principal.tenant_id,
        channel_account_id=account_id,
        is_active=False,
    )
    await record_audit_event(
        session=session,
        tenant_id=principal.tenant_id,
        actor_type="human_user",
        actor_id=str(principal.user_id),
        action="channel.disconnected",
        resource_type="channel_account",
        resource_id=account_id,
        before_state={"status": "active"},
        after_state={"status": ChannelStatus.DISCONNECTED.value},
    )
    return DisconnectChannelResponse(
        channel_account_id=account_id, status=ChannelStatus.DISCONNECTED
    )


@email_router.post("/email/connect", response_model=ConnectMessagingResponse)
async def connect_email(
    payload: EmailConnectRequest,
    request: Request,
    principal: ChannelConnectorPrincipal,
) -> ConnectMessagingResponse:
    normalized_email = str(payload.email_address).strip().lower()
    credentials: dict[str, Any] = {}
    if payload.provider == "resend":
        if payload.api_key is None:
            raise invalid_connection("An API key is required for the email provider.")
        credentials["api_key"] = payload.api_key.get_secret_value()
    else:
        if payload.smtp_host is None or payload.smtp_password is None:
            raise invalid_connection("SMTP host and password are required for SMTP/IMAP.")
        # Everything the delivery engine needs is stored in the encrypted envelope so a
        # send never has to re-resolve the account config before talking to the MTA.
        credentials["provider"] = "smtp_imap"
        credentials["smtp_host"] = payload.smtp_host
        credentials["smtp_port"] = payload.smtp_port
        credentials["smtp_username"] = payload.smtp_username or normalized_email
        credentials["smtp_password"] = payload.smtp_password.get_secret_value()
    display_name = (payload.display_name or "").strip() or normalized_email

    config_patch = {
        "provider": payload.provider,
        "smtp_host": payload.smtp_host,
        "smtp_port": payload.smtp_port,
        "imap_host": payload.imap_host,
        "imap_port": payload.imap_port,
    }
    tenant = principal.tenant_id
    async with tenant_session(messaging_sessions(request), tenant) as session:
        owner_uid, vis_scope = await resolve_connect_ownership(
            session,
            principal,
            payload.visibility_scope,
            payload.owner_user_id,
            allow_agent_self_bind=True,
        )
        aid = await register_messaging_account(
            session,
            tenant,
            ChannelType.EMAIL,
            normalized_email,
            display_name,
            owner_user_id=owner_uid,
            visibility_scope=vis_scope,
            actor_user_id=principal.user_id,
            actor_is_admin=principal.role in (UserRole.OWNER, UserRole.ADMIN),
            config_patch=config_patch,
        )
        # Secrets never enter ``config`` or logs: they are envelope-encrypted per account.
        await save_messaging_credentials(session, tenant, aid, credentials)
        await record_audit_event(
            session=session,
            tenant_id=tenant,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.email_connected",
            resource_type="channel_account",
            resource_id=aid,
            after_state={
                "provider": payload.provider,
                "email_address": normalized_email,
                "owner_user_id": str(owner_uid) if owner_uid else None,
                "visibility_scope": vis_scope,
            },
        )
    return ConnectMessagingResponse(
        channel_account_id=aid,
        external_account_id=normalized_email,
        display_name=display_name,
        owner_user_id=owner_uid,
        visibility_scope=vis_scope,
    )


router.include_router(email_router)


@router.post("/web-livechat/connect", response_model=ConnectMessagingResponse)
async def connect_web_livechat(
    payload: WebLivechatConnectRequest,
    request: Request,
    principal: Admin,
) -> ConnectMessagingResponse:
    """Register a website LiveChat widget so the UI can render the embed snippet.

    The normalized domain doubles as the ``site_key`` the widget resolves its tenant from.
    """
    normalized_domain = normalize_site_domain(payload.site_domain)
    effective_name = (payload.display_name or "").strip() or f"LiveChat · {normalized_domain}"
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
            ChannelType.WEB_LIVECHAT,
            normalized_domain,
            effective_name,
            owner_user_id=owner_uid,
            visibility_scope=vis_scope,
            actor_user_id=principal.user_id,
            actor_is_admin=True,
            config_patch={"site_domain": normalized_domain},
        )
        await record_audit_event(
            session=session,
            tenant_id=tenant,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.web_livechat_connected",
            resource_type="channel_account",
            resource_id=aid,
            after_state={
                "site_domain": normalized_domain,
                "owner_user_id": str(owner_uid) if owner_uid else None,
                "visibility_scope": vis_scope,
            },
        )
    return ConnectMessagingResponse(
        channel_account_id=aid,
        external_account_id=normalized_domain,
        display_name=effective_name,
        owner_user_id=owner_uid,
        visibility_scope=vis_scope,
    )


@router.get("/dlq")
async def list_channel_dlq(
    session: Db, principal: Admin, page: Page
) -> PaginatedResponse[ChannelDlqRead]:
    table = DeadLetterEvent
    query = (
        select(
            table.id,
            table.channel_type,
            table.external_event_id,
            table.error_detail.label("error_message"),
            table.attempt_count.label("retry_count"),
            table.created_at,
        )
        .where(table.tenant_id == principal.tenant_id, table.status == "open")
        .order_by(table.created_at.desc(), table.id)
    )
    return await _page(session, query, page, ChannelDlqRead)


@router.get("/consents")
async def list_channel_consents(
    session: Db, principal: Admin, page: Page
) -> PaginatedResponse[ChannelConsentRead]:
    table = ConsentRecord
    # Correlated lookup keeps the channels module free of identity ORM imports.
    customer_name: Any = literal_column(
        "(SELECT display_name FROM customers WHERE customers.id = consent_records.customer_id)"
    ).label("customer_name")
    query = (
        select(
            table.id,
            customer_name,
            table.channel_type,
            table.consent_status,
            table.proof_source,
            table.recorded_at,
        )
        .where(table.tenant_id == principal.tenant_id)
        .order_by(table.recorded_at.desc(), table.id)
    )
    return await _page(session, query, page, ChannelConsentRead)


@router.get("/suppressions")
async def list_channel_suppressions(
    session: Db, principal: Admin, page: Page
) -> PaginatedResponse[ChannelSuppressionRead]:
    table = SuppressionEntry
    query = (
        select(
            table.id,
            table.channel_type,
            table.normalized_address,
            table.reason,
            table.created_at,
        )
        .where(table.tenant_id == principal.tenant_id)
        .order_by(table.created_at.desc(), table.id)
    )
    return await _page(session, query, page, ChannelSuppressionRead)
