from __future__ import annotations

from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.crm.models import CustomerFollowup, Lead, Opportunity, TaskReminder
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import OpportunityCreate, OpportunityRead
from erp_core.crm.workbench_schemas import (
    FollowupCreate,
    FollowupRead,
    FollowupUpdate,
    LeadUpdate,
    TaskCreate,
    TaskRead,
    TaskStatus,
    TaskUpdate,
)
from erp_core.shared.enums import LeadStatus, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, page_of, record_not_found
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/b2b", tags=["b2b"])
SalesPrincipal = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)),
]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db, scope="function")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


async def require_customer(session: AsyncSession, tenant_id: UUID, customer_id: UUID) -> None:
    # Identity owns the customer aggregate: check it with the RLS-scoped query rather than
    # importing another module's ORM model (enforced by test_no_cross_module_orm_model_imports).
    customer = await session.scalar(
        sa.text(
            "SELECT id FROM customers WHERE tenant_id = :tid AND id = :cid "
            "AND merged_into_customer_id IS NULL"
        ),
        {"tid": tenant_id, "cid": customer_id},
    )
    if customer is None:
        raise record_not_found("Customer")


async def require_member(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> None:
    member = await session.scalar(
        sa.text(
            "SELECT id FROM tenant_memberships WHERE tenant_id = :tid AND user_id = :uid "
            "AND is_active = true"
        ),
        {"tid": tenant_id, "uid": user_id},
    )
    if member is None:
        raise record_not_found("Workspace member")


async def update_lead(
    session: AsyncSession,
    tenant_id: UUID,
    lead_id: UUID,
    payload: LeadUpdate,
) -> Lead:
    lead = await session.scalar(
        sa.select(Lead).where(Lead.tenant_id == tenant_id, Lead.id == lead_id).with_for_update()
    )
    if lead is None:
        raise record_not_found("Lead")
    if payload.status is not None and payload.status != lead.status:
        if LeadStatus.CONVERTED in (payload.status, lead.status):
            raise conflicting_change("Only opportunity creation can convert a lead.")
        lead.status = payload.status
    if payload.qualification_score is not None:
        lead.qualification_score = payload.qualification_score
    await session.flush()
    return lead


async def create_scoped_opportunity(
    session: AsyncSession,
    tenant_id: UUID,
    payload: OpportunityCreate,
    service: QuoteService,
) -> OpportunityRead:
    await require_customer(session, tenant_id, payload.customer_id)
    await require_member(session, tenant_id, payload.owner_user_id)
    if (
        payload.company_id is not None
        and await session.scalar(
            sa.text("SELECT id FROM b2b_companies WHERE tenant_id = :tid AND id = :id"),
            {"tid": tenant_id, "id": payload.company_id},
        )
        is None
    ):
        raise record_not_found("Company")
    if payload.lead_id is not None:
        lead = await session.scalar(
            sa.select(Lead)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.id == payload.lead_id,
                Lead.customer_id == payload.customer_id,
            )
            .with_for_update()
        )
        if lead is None:
            raise record_not_found("Lead for this customer")
        if lead.status in (LeadStatus.CONVERTED, LeadStatus.DISQUALIFIED):
            raise conflicting_change("This lead cannot be converted again or is disqualified.")
        lead.status = LeadStatus.CONVERTED
    return await service.create_opportunity(session, tenant_id, payload)


@router.get("/followups")
async def list_followups(
    principal: SalesPrincipal,
    session: TenantDB,
    customer_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[FollowupRead]:
    stmt = sa.select(CustomerFollowup).where(CustomerFollowup.tenant_id == principal.tenant_id)
    if customer_id is not None:
        stmt = stmt.where(CustomerFollowup.customer_id == customer_id)
    return await page_of(
        session,
        stmt.order_by(CustomerFollowup.created_at.desc(), CustomerFollowup.id),
        FollowupRead.model_validate,
        limit,
        offset,
    )


@router.post("/followups", status_code=201)
async def create_followup(
    principal: SalesPrincipal,
    session: TenantDB,
    payload: FollowupCreate,
) -> FollowupRead:
    await require_customer(session, principal.tenant_id, payload.customer_id)
    if (
        payload.opportunity_id is not None
        and await session.scalar(
            sa.select(Opportunity.id).where(
                Opportunity.tenant_id == principal.tenant_id,
                Opportunity.id == payload.opportunity_id,
                Opportunity.customer_id == payload.customer_id,
            )
        )
        is None
    ):
        raise record_not_found("Opportunity for this customer")
    row = CustomerFollowup(
        tenant_id=principal.tenant_id, author_user_id=principal.user_id, **payload.model_dump()
    )
    session.add(row)
    await session.flush()
    return FollowupRead.model_validate(row)


@router.patch("/followups/{followup_id}")
async def update_followup(
    principal: SalesPrincipal,
    session: TenantDB,
    followup_id: UUID,
    payload: FollowupUpdate,
) -> FollowupRead:
    row = await session.scalar(
        sa.select(CustomerFollowup)
        .where(
            CustomerFollowup.tenant_id == principal.tenant_id,
            CustomerFollowup.id == followup_id,
        )
        .with_for_update()
    )
    if row is None:
        raise record_not_found("Follow-up")
    if principal.role == UserRole.AGENT and row.author_user_id != principal.user_id:
        raise record_not_found("Follow-up")
    if payload.followup_type is not None:
        row.followup_type = payload.followup_type
    if payload.summary is not None:
        row.summary = payload.summary
    if "next_action_at" in payload.model_fields_set:
        row.next_action_at = payload.next_action_at
    await session.flush()
    return FollowupRead.model_validate(row)


@router.get("/tasks")
async def list_tasks(
    principal: SalesPrincipal,
    session: TenantDB,
    task_status: TaskStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[TaskRead]:
    stmt = sa.select(TaskReminder).where(TaskReminder.tenant_id == principal.tenant_id)
    if task_status is not None:
        stmt = stmt.where(TaskReminder.status == task_status)
    return await page_of(
        session,
        stmt.order_by(
            TaskReminder.due_at.asc().nulls_last(), TaskReminder.created_at.desc(), TaskReminder.id
        ),
        TaskRead.model_validate,
        limit,
        offset,
    )


@router.post("/tasks", status_code=201)
async def create_task(
    principal: SalesPrincipal, session: TenantDB, payload: TaskCreate
) -> TaskRead:
    if payload.assignee_user_id is not None:
        await require_member(session, principal.tenant_id, payload.assignee_user_id)
    if payload.linked_entity_type is not None:
        # customer lives in identity; the rest are CRM-owned aggregates.
        tables = {
            "customer": "customers",
            "lead": "leads",
            "opportunity": "opportunities",
            "quote": "quotes",
        }
        linked = await session.scalar(
            sa.text(
                f"SELECT id FROM {tables[payload.linked_entity_type]} "
                "WHERE tenant_id = :tid AND id = :id"
            ),
            {"tid": principal.tenant_id, "id": payload.linked_entity_id},
        )
        if linked is None:
            raise record_not_found("Linked entity")
    row = TaskReminder(tenant_id=principal.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    return TaskRead.model_validate(row)


@router.patch("/tasks/{task_id}")
async def update_task(
    principal: SalesPrincipal,
    session: TenantDB,
    task_id: UUID,
    payload: TaskUpdate,
) -> TaskRead:
    row = await session.scalar(
        sa.select(TaskReminder)
        .where(
            TaskReminder.tenant_id == principal.tenant_id,
            TaskReminder.id == task_id,
        )
        .with_for_update()
    )
    if row is None:
        raise record_not_found("Task")
    if payload.assignee_user_id is not None:
        await require_member(session, principal.tenant_id, payload.assignee_user_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None or field in ("assignee_user_id", "due_at"):
            setattr(row, field, value)
    await session.flush()
    return TaskRead.model_validate(row)
