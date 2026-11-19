from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.models import (
    ChannelAccount,
    ConsentRecord,
    DeadLetterEvent,
    SuppressionEntry,
)
from erp_core.shared.enums import ChannelStatus, ChannelType, UserRole
from erp_core.shared.pagination import PaginatedResponse, PaginationParams
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])
Admin = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
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
    session: Db, principal: Admin, page: Page
) -> PaginatedResponse[ChannelAccountRead]:
    table = ChannelAccount
    query = (
        select(
            table.id,
            table.channel_type,
            table.external_account_id,
            table.display_name,
            table.status,
            table.rate_limit_profile,
        )
        .where(table.tenant_id == principal.tenant_id)
        .order_by(table.created_at.desc(), table.id)
    )
    return await _page(session, query, page, ChannelAccountRead)


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
    customer_name: Any = text(
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
