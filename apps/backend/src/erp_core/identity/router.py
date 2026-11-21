from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Select, TextClause, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.identity.models import Customer, CustomerIdentity
from erp_core.identity.schemas import (
    CustomerIdentityRead,
    CustomerRead,
    CustomerUpdateRequest,
    MergeCustomersRequest,
    MergeCustomersResponse,
    SplitCustomerRequest,
    SplitCustomerResponse,
)
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import CustomerType, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

customer_router = APIRouter(prefix="/api/v1/customers", tags=["identity"])

_require_customer_reader = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE
)
_require_customer_editor = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
)
_require_customer_splitter = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER
)

# ---------------------------------------------------------------------------------------
# AGENT personal-mailbox isolation for the customer directory.
#
# An AGENT sees a customer when EITHER
#   (a) at least one conversation for that customer is workable by them
#       (assigned to them, or unassigned on an unowned SHARED mailbox), OR
#   (b) the customer has no conversations at all (never touched a private mailbox).
# This intentionally mirrors `channels.inbox_service.agent_can_access_conversation`, but
# stays plain tenant-scoped SQL: `identity_isolated` forbids importing erp_core.channels.
_AGENT_VISIBLE_CUSTOMER_SQL = """
    (
        EXISTS (
            SELECT 1
            FROM conversations conv
            JOIN channel_accounts ca
              ON ca.id = conv.channel_account_id
             AND ca.tenant_id = conv.tenant_id
            WHERE conv.tenant_id = :visibility_tenant_id
              AND conv.customer_id = {customer_ref}
              AND (
                    conv.assigned_user_id = :visibility_user_id
                    OR (
                        conv.assigned_user_id IS NULL
                        AND ca.visibility_scope = 'shared'
                        AND ca.owner_user_id IS NULL
                    )
                  )
        )
        OR NOT EXISTS (
            SELECT 1
            FROM conversations any_conv
            WHERE any_conv.tenant_id = :visibility_tenant_id
              AND any_conv.customer_id = {customer_ref}
        )
    )
"""


def _agent_customer_visibility_clause(
    customer_ref: str, *, tenant_id: UUID, user_id: UUID
) -> TextClause:
    """Build the AGENT visibility predicate bound to this tenant and salesperson."""
    return text(_AGENT_VISIBLE_CUSTOMER_SQL.format(customer_ref=customer_ref)).bindparams(
        visibility_tenant_id=tenant_id, visibility_user_id=user_id
    )


def _scoped_customer_query(
    principal: CurrentPrincipal, customer_id: UUID
) -> Select[tuple[Customer]]:
    """Tenant-scoped single-customer lookup honouring AGENT mailbox isolation."""
    stmt = select(Customer).where(
        Customer.tenant_id == principal.tenant_id,
        Customer.id == customer_id,
    )
    if principal.role == UserRole.AGENT:
        stmt = stmt.where(
            _agent_customer_visibility_clause(
                "customers.id",
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
            )
        )
    return stmt


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


@customer_router.get("", response_model=PaginatedResponse[CustomerRead])
async def list_customers(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_customer_reader)],
    q: Annotated[str | None, Query()] = None,
    customer_type: Annotated[CustomerType | None, Query()] = None,
    include_merged: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[CustomerRead]:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    now_utc = datetime.now(UTC)

    async with tenant_session(session_factory, principal.tenant_id) as session:
        stmt = select(Customer).where(Customer.tenant_id == principal.tenant_id)
        if not include_merged:
            stmt = stmt.where(Customer.merged_into_customer_id.is_(None))
        if customer_type is not None:
            stmt = stmt.where(Customer.customer_type == customer_type.value)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Customer.display_name.ilike(pattern),
                    Customer.primary_email.ilike(pattern),
                    Customer.primary_phone_e164.ilike(pattern),
                )
            )
        if principal.role == UserRole.AGENT:
            stmt = stmt.where(
                _agent_customer_visibility_clause(
                    "customers.id",
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        customers = (
            await session.execute(
                stmt.order_by(Customer.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()

        cids = [c.id for c in customers]
        identities_by_cid: dict[UUID, list[CustomerIdentityRead]] = {cid: [] for cid in cids}
        if cids:
            ident_rows = (
                await session.execute(
                    select(CustomerIdentity).where(
                        CustomerIdentity.tenant_id == principal.tenant_id,
                        CustomerIdentity.customer_id.in_(cids),
                    )
                )
            ).scalars().all()
            for ident in ident_rows:
                identities_by_cid[ident.customer_id].append(
                    CustomerIdentityRead.model_validate(ident)
                )

        items = [
            CustomerRead(
                id=c.id,
                tenant_id=c.tenant_id,
                customer_type=CustomerType(c.customer_type),
                company_id=c.company_id,
                display_name=c.display_name,
                primary_email=c.primary_email,
                primary_phone_e164=c.primary_phone_e164,
                country_code=c.country_code,
                postal_code=c.postal_code,
                preferred_language=c.preferred_language,
                iana_timezone=c.iana_timezone,
                timezone_source=c.timezone_source,
                lifecycle_stage=c.lifecycle_stage,
                ltv_usd=c.ltv_usd,
                merged_into_customer_id=c.merged_into_customer_id,
                created_at=c.created_at,
                buyer_local_time=service.compute_buyer_local_time(c.iana_timezone, now_utc),
                identities=identities_by_cid.get(c.id, []),
                merge_proposals=[],
            )
            for c in customers
        ]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )


@customer_router.get("/{customer_id}", response_model=CustomerRead)
async def get_customer(
    customer_id: UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_customer_reader)],
) -> CustomerRead:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        cust = (
            await session.execute(_scoped_customer_query(principal, customer_id))
        ).scalar_one_or_none()
        if cust is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )
        ident_rows = (
            await session.execute(
                select(CustomerIdentity).where(
                    CustomerIdentity.tenant_id == principal.tenant_id,
                    CustomerIdentity.customer_id == customer_id,
                )
            )
        ).scalars().all()
        proposals = await service.list_merge_proposals(session, principal.tenant_id, customer_id)
        return CustomerRead(
            id=cust.id,
            tenant_id=cust.tenant_id,
            customer_type=CustomerType(cust.customer_type),
            company_id=cust.company_id,
            display_name=cust.display_name,
            primary_email=cust.primary_email,
            primary_phone_e164=cust.primary_phone_e164,
            country_code=cust.country_code,
            postal_code=cust.postal_code,
            preferred_language=cust.preferred_language,
            iana_timezone=cust.iana_timezone,
            timezone_source=cust.timezone_source,
            lifecycle_stage=cust.lifecycle_stage,
            ltv_usd=cust.ltv_usd,
            merged_into_customer_id=cust.merged_into_customer_id,
            created_at=cust.created_at,
            buyer_local_time=service.compute_buyer_local_time(cust.iana_timezone),
            identities=[CustomerIdentityRead.model_validate(i) for i in ident_rows],
            merge_proposals=proposals,
        )


@customer_router.patch("/{customer_id}", response_model=CustomerRead)
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdateRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_customer_editor)],
) -> CustomerRead:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        cust = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == principal.tenant_id,
                    Customer.id == customer_id,
                )
            )
        ).scalar_one_or_none()
        if cust is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )

        if payload.display_name is not None:
            cust.display_name = payload.display_name.strip()
        if payload.customer_type is not None:
            cust.customer_type = payload.customer_type.value
        if payload.company_id is not None:
            cust.company_id = payload.company_id
        if payload.preferred_language is not None:
            cust.preferred_language = payload.preferred_language.strip()
        if payload.iana_timezone is not None:
            tz_info = service.compute_buyer_local_time(payload.iana_timezone)
            if tz_info.iana_timezone is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid IANA timezone",
                )
            cust.iana_timezone = tz_info.iana_timezone
            cust.timezone_source = "manual"
        if payload.postal_code is not None:
            cust.postal_code = payload.postal_code.strip()
        if payload.lifecycle_stage is not None:
            cust.lifecycle_stage = payload.lifecycle_stage.strip()
        await session.flush()

        ident_rows = (
            await session.execute(
                select(CustomerIdentity).where(
                    CustomerIdentity.tenant_id == principal.tenant_id,
                    CustomerIdentity.customer_id == customer_id,
                )
            )
        ).scalars().all()
        proposals = await service.list_merge_proposals(session, principal.tenant_id, customer_id)
        return CustomerRead(
            id=cust.id,
            tenant_id=cust.tenant_id,
            customer_type=CustomerType(cust.customer_type),
            company_id=cust.company_id,
            display_name=cust.display_name,
            primary_email=cust.primary_email,
            primary_phone_e164=cust.primary_phone_e164,
            country_code=cust.country_code,
            postal_code=cust.postal_code,
            preferred_language=cust.preferred_language,
            iana_timezone=cust.iana_timezone,
            timezone_source=cust.timezone_source,
            lifecycle_stage=cust.lifecycle_stage,
            ltv_usd=cust.ltv_usd,
            merged_into_customer_id=cust.merged_into_customer_id,
            created_at=cust.created_at,
            buyer_local_time=service.compute_buyer_local_time(cust.iana_timezone),
            identities=[CustomerIdentityRead.model_validate(i) for i in ident_rows],
            merge_proposals=proposals,
        )


@customer_router.post("/merge", response_model=MergeCustomersResponse)
async def merge_customers_endpoint(
    payload: MergeCustomersRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_customer_editor)],
) -> MergeCustomersResponse:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            merge_event_id = await service.merge_customers(
                session=session,
                tenant_id=principal.tenant_id,
                source_customer_id=payload.source_customer_id,
                target_customer_id=payload.target_customer_id,
                performed_by=str(principal.user_id),
                reason=payload.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return MergeCustomersResponse(
        merge_event_id=merge_event_id,
        source_customer_id=payload.source_customer_id,
        target_customer_id=payload.target_customer_id,
    )


@customer_router.post("/split", response_model=SplitCustomerResponse)
async def split_customers_endpoint(
    payload: SplitCustomerRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(_require_customer_splitter)],
) -> SplitCustomerResponse:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            split_event_id = await service.split_customer_identity(
                session=session,
                tenant_id=principal.tenant_id,
                merge_event_id=payload.merge_event_id,
                performed_by=str(principal.user_id),
                reason=payload.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return SplitCustomerResponse(
        split_event_id=split_event_id,
        merge_event_id=payload.merge_event_id,
    )
