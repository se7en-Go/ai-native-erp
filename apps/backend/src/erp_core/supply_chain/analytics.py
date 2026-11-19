from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.crm.workbench_schemas import TaskRead
from erp_core.shared.enums import OpportunityStage, UserRole
from erp_core.shared.query import escape_like
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(tags=["analytics"])
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db, scope="function")]


class CurrencyValue(BaseModel):
    currency: str
    amount: Decimal


class ChannelVolume(BaseModel):
    channel: str
    conversations: int


class FunnelStage(BaseModel):
    stage: OpportunityStage
    count: int


class FinancialKpis(BaseModel):
    currency: str
    cost: Decimal
    net_contribution_margin: Decimal


class DashboardRead(BaseModel):
    open_inquiries: int
    pipeline_value: list[CurrencyValue]
    pending_proposals: int
    b2c_orders: int
    low_stock_alerts: int
    open_tasks: int
    upcoming_tasks: list[TaskRead]
    channels: list[ChannelVolume]
    funnel: list[FunnelStage]
    financials: list[FinancialKpis] | None = Field(default=None, exclude_if=lambda v: v is None)


class SkuMargin(BaseModel):
    variant_id: UUID
    sku: str
    currency: str
    units: int
    revenue: Decimal
    cost: Decimal
    gross_margin: Decimal


class OverviewRead(BaseModel):
    channels: list[ChannelVolume]
    funnel: list[FunnelStage]
    won_conversion_pct: Decimal
    sku_margins: list[SkuMargin] | None = Field(default=None, exclude_if=lambda v: v is None)


class SearchHit(BaseModel):
    kind: Literal["customer", "order", "quote", "sku"]
    id: UUID
    label: str


class SearchRead(BaseModel):
    items: list[SearchHit]


async def _channels(session: AsyncSession, tenant_id: UUID) -> list[ChannelVolume]:
    rows = (
        await session.execute(
            text("""
        SELECT channel_type AS channel, count(*) AS conversations
        FROM conversations WHERE tenant_id = :tid GROUP BY channel_type
        ORDER BY conversations DESC, channel_type
    """),
            {"tid": tenant_id},
        )
    ).mappings()
    return [ChannelVolume.model_validate(row) for row in rows]


async def _funnel(session: AsyncSession, tenant_id: UUID) -> list[FunnelStage]:
    rows = await session.execute(
        text("SELECT stage, count(*) FROM opportunities WHERE tenant_id = :tid GROUP BY stage"),
        {"tid": tenant_id},
    )
    counts = {str(stage): int(count) for stage, count in rows}
    return [
        FunnelStage(stage=stage, count=counts.get(stage.value, 0)) for stage in OpportunityStage
    ]


@router.get("/analytics/dashboard")
async def dashboard(principal: Reader, session: TenantDB) -> DashboardRead:
    params = {"tid": principal.tenant_id}
    counts = (
        (
            await session.execute(
                text("""
        SELECT
          (SELECT count(*) FROM conversations WHERE tenant_id = :tid
           AND conversation_mode IN ('b2b_inquiry', 'mixed')
           AND status IN ('open', 'pending_human')) AS open_inquiries,
          (SELECT count(*) FROM ai_proposals WHERE tenant_id = :tid AND status = 'pending'
           AND (expires_at IS NULL OR expires_at > now())) AS pending_proposals,
          (SELECT count(*) FROM orders WHERE tenant_id = :tid AND order_mode = 'b2c_retail')
            AS b2c_orders,
          (SELECT count(*) FROM vw_tenant_inventory_risk_summary WHERE tenant_id = :tid
           AND available_qty <= 5) AS low_stock_alerts,
          (SELECT count(*) FROM task_reminders WHERE tenant_id = :tid AND status = 'open')
            AS open_tasks
    """),
                params,
            )
        )
        .mappings()
        .one()
    )
    pipeline = (
        await session.execute(
            text("""
        SELECT currency, sum(expected_amount) AS amount FROM opportunities
        WHERE tenant_id = :tid AND stage NOT IN ('closed_won', 'closed_lost')
        GROUP BY currency ORDER BY currency
    """),
            params,
        )
    ).mappings()
    tasks: list[TaskRead] = []
    if principal.role != UserRole.FINANCE:
        rows = (
            await session.execute(
                text("""
            SELECT * FROM task_reminders WHERE tenant_id = :tid AND status = 'open'
            ORDER BY due_at NULLS LAST, created_at DESC, id LIMIT 5
        """),
                params,
            )
        ).mappings()
        tasks = [TaskRead.model_validate(row) for row in rows]
    financials: list[FinancialKpis] | None = None
    if principal.role != UserRole.AGENT:
        aggregates = (
            await session.execute(
                text("""
            SELECT base_currency AS currency, sum(cogs_base) AS cost,
                   sum(net_contribution_margin_base) AS net_contribution_margin
            FROM order_profit_snapshots WHERE tenant_id = :tid
            GROUP BY base_currency ORDER BY base_currency
        """),
                params,
            )
        ).mappings()
        financials = [FinancialKpis.model_validate(row) for row in aggregates]
    return DashboardRead(
        **counts,
        pipeline_value=[CurrencyValue.model_validate(row) for row in pipeline],
        upcoming_tasks=tasks,
        channels=await _channels(session, principal.tenant_id),
        funnel=await _funnel(session, principal.tenant_id),
        financials=financials,
    )


@router.get("/analytics/overview")
async def overview(principal: Reader, session: TenantDB) -> OverviewRead:
    funnel = await _funnel(session, principal.tenant_id)
    total = sum(row.count for row in funnel)
    won = next(row.count for row in funnel if row.stage == OpportunityStage.CLOSED_WON)
    conversion = (Decimal(won * 100) / Decimal(total or 1)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    margins: list[SkuMargin] | None = None
    if principal.role != UserRole.AGENT:
        rows = (
            await session.execute(
                text("""
            WITH gross AS (
                SELECT i.variant_id, i.sku_snapshot AS sku, s.base_currency AS currency,
                       sum(i.quantity) AS units,
                       sum(i.line_total * o.fx_rate_to_base) AS revenue,
                       sum(i.unit_cost_base_snapshot * i.quantity) AS cost
                FROM order_items i
                JOIN orders o ON i.tenant_id = o.tenant_id AND i.order_id = o.id
                JOIN order_profit_snapshots s ON s.tenant_id = o.tenant_id AND s.order_id = o.id
                WHERE i.tenant_id = :tid AND o.fulfillment_status NOT IN ('cancelled', 'refunded')
                GROUP BY i.variant_id, i.sku_snapshot, s.base_currency
            )
            SELECT variant_id, sku, currency, units, round(revenue, 2) AS revenue,
                   round(cost, 2) AS cost, round(revenue - cost, 2) AS gross_margin
            FROM gross ORDER BY units DESC, sku, currency, variant_id LIMIT 100
        """),
                {"tid": principal.tenant_id},
            )
        ).mappings()
        margins = [SkuMargin.model_validate(row) for row in rows]
    return OverviewRead(
        channels=await _channels(session, principal.tenant_id),
        funnel=funnel,
        won_conversion_pct=conversion,
        sku_margins=margins,
    )


@router.get("/search")
async def global_search(
    principal: Reader,
    session: TenantDB,
    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=40)] = 20,
) -> SearchRead:
    if len(q.strip()) < 2:
        return SearchRead(items=[])
    prefix = escape_like(q.strip())
    rows = (
        await session.execute(
            text("""
        SELECT kind, id, label FROM (
            SELECT 'customer' AS kind, id, display_name AS label FROM customers
            WHERE tenant_id = :tid AND merged_into_customer_id IS NULL
              AND (display_name ILIKE :contains ESCAPE '!'
                   OR primary_email ILIKE :contains ESCAPE '!'
                   OR primary_phone_e164 ILIKE :contains ESCAPE '!')
            UNION ALL
            SELECT 'order', id, order_no FROM orders
            WHERE tenant_id = :tid AND order_no ILIKE :contains ESCAPE '!'
            UNION ALL
            SELECT 'quote', id, concat(quote_no, ' · v', version) FROM quotes
            WHERE tenant_id = :tid AND :quotes AND quote_no ILIKE :contains ESCAPE '!'
            UNION ALL
            SELECT 'sku', id, sku FROM product_variants
            WHERE tenant_id = :tid AND sku ILIKE :contains ESCAPE '!'
        ) hits
        ORDER BY label ILIKE :prefix ESCAPE '!' DESC, lower(label), kind, id LIMIT :limit
    """),
            {
                "tid": principal.tenant_id,
                "contains": f"%{prefix}%",
                "prefix": f"{prefix}%",
                "limit": limit,
                "quotes": principal.role != UserRole.FINANCE,
            },
        )
    ).mappings()
    return SearchRead(items=[SearchHit.model_validate(row) for row in rows])
