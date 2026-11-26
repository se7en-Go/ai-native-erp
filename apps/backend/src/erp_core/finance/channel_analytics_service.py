"""Multi-platform sales, margin, ROAS & SKU velocity analytics service."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.schemas import (
    ChannelAnalyticsOverviewRead,
    ChannelBreakdownItem,
    ChannelDailyMetricRead,
    ChannelDailyMetricUpsert,
    ChannelDailyTrendPoint,
    SkuChannelBreakdownItem,
    SkuVelocityItem,
    SkuVelocityResponse,
)
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError

_WINDOW_DAYS: dict[str, int] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
}


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def parse_analytics_window(window: str) -> tuple[str, int]:
    cleaned = window.strip().lower()
    days = _WINDOW_DAYS.get(cleaned)
    if days is None:
        raise AppError(
            slug="invalid-analytics-window",
            status_code=422,
            code="ERR_INVALID_ANALYTICS_WINDOW",
            title="Invalid Analytics Window",
            detail="Window must be one of: 7d, 30d, 90d.",
        )
    return cleaned, days


def _finalize_breakdown_item(
    *,
    channel_key: str,
    channel_account_id: UUID | None,
    channel_type: str,
    channel_label: str,
    order_count: int,
    units_sold: int,
    gmv_base: Decimal,
    refunded_amount_base: Decimal,
    cogs_base: Decimal,
    platform_commission_base: Decimal,
    ad_spend_base: Decimal,
    net_margin_base: Decimal,
) -> ChannelBreakdownItem:
    gmv_q = _q2(gmv_base)
    refunded_q = _q2(refunded_amount_base)
    ad_q = _q2(ad_spend_base)
    net_q = _q2(net_margin_base)
    refund_rate = (
        _q2(refunded_q / gmv_q * Decimal("100"))
        if gmv_q > Decimal("0")
        else Decimal("0.00")
    )
    margin_pct = (
        _q2(net_q / gmv_q * Decimal("100"))
        if gmv_q > Decimal("0")
        else Decimal("0.00")
    )
    roas = _q2(gmv_q / ad_q) if ad_q > Decimal("0") else None
    return ChannelBreakdownItem(
        channel_key=channel_key,
        channel_account_id=channel_account_id,
        channel_type=channel_type,
        channel_label=channel_label,
        order_count=order_count,
        units_sold=units_sold,
        gmv_base=gmv_q,
        refunded_amount_base=refunded_q,
        refund_rate_pct=refund_rate,
        cogs_base=_q2(cogs_base),
        platform_commission_base=_q2(platform_commission_base),
        ad_spend_base=ad_q,
        net_margin_base=net_q,
        margin_pct=margin_pct,
        roas=roas,
    )


class ChannelAnalyticsService:
    """Decimal-exact cross-channel revenue, margin, ROAS, and replenishment engine."""

    @staticmethod
    async def _tenant_base_currency(session: AsyncSession, tenant_id: UUID) -> str:
        val = await session.scalar(
            text("SELECT base_currency FROM tenants WHERE id = :tid"),
            {"tid": tenant_id},
        )
        return str(val or "CNY")

    async def get_overview(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        window: str = "30d",
    ) -> ChannelAnalyticsOverviewRead:
        norm_window, days = parse_analytics_window(window)
        now = datetime.now(UTC)
        since_ts = now - timedelta(days=days)
        since_date = (now - timedelta(days=days - 1)).date()
        base_currency = await self._tenant_base_currency(session, tenant_id)

        order_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        o.id,
                        o.channel_account_id,
                        COALESCE(ca.channel_type, 'direct_b2b') AS channel_type,
                        COALESCE(ca.display_name, 'Direct B2B') AS channel_label,
                        (o.created_at AT TIME ZONE 'UTC')::date AS order_date,
                        o.total_amount,
                        o.fx_rate_to_base,
                        o.fulfillment_status,
                        o.payment_status,
                        COALESCE(
                            NULLIF(o.channel_metadata->>'refunded_amount', ''), '0'
                        ) AS refunded_raw,
                        COALESCE(
                            NULLIF(o.channel_metadata->>'platform_commission_base', ''), '0'
                        ) AS meta_comm_raw,
                        COALESCE(
                            NULLIF(o.channel_metadata->>'ad_attribution_cost_base', ''), '0'
                        ) AS meta_ad_raw,
                        ops.cogs_base AS snap_cogs_base,
                        ops.platform_commission_base AS snap_comm_base,
                        ops.ad_attribution_cost_base AS snap_ad_base,
                        ops.net_contribution_margin_base AS snap_net_margin_base,
                        COALESCE(oi_agg.units, 0) AS units_sold,
                        COALESCE(oi_agg.cogs_raw, 0) AS fallback_cogs_base
                    FROM orders o
                    LEFT JOIN channel_accounts ca
                      ON ca.id = o.channel_account_id AND ca.tenant_id = o.tenant_id
                    LEFT JOIN order_profit_snapshots ops
                      ON ops.order_id = o.id AND ops.tenant_id = o.tenant_id
                    LEFT JOIN (
                        SELECT tenant_id, order_id,
                               COALESCE(SUM(quantity), 0) AS units,
                               COALESCE(SUM(quantity * unit_cost_base_snapshot), 0) AS cogs_raw
                        FROM order_items
                        WHERE tenant_id = :tid
                        GROUP BY tenant_id, order_id
                    ) oi_agg
                      ON oi_agg.order_id = o.id AND oi_agg.tenant_id = o.tenant_id
                    WHERE o.tenant_id = :tid AND o.created_at >= :since_ts
                    ORDER BY o.created_at ASC
                    """
                ),
                {"tid": tenant_id, "since_ts": since_ts},
            )
        ).all()

        metric_rows = (
            await session.execute(
                text(
                    """
                    SELECT cdm.channel_account_id,
                           ca.channel_type,
                           ca.display_name AS channel_label,
                           cdm.metric_date,
                           COALESCE(SUM(cdm.ad_spend_base), 0) AS ad_spend_base
                    FROM channel_daily_metrics cdm
                    JOIN channel_accounts ca
                      ON ca.id = cdm.channel_account_id AND ca.tenant_id = cdm.tenant_id
                    WHERE cdm.tenant_id = :tid AND cdm.metric_date >= :since_date
                    GROUP BY cdm.channel_account_id, ca.channel_type,
                             ca.display_name, cdm.metric_date
                    """
                ),
                {"tid": tenant_id, "since_date": since_date},
            )
        ).all()

        acc: dict[str, dict[str, Any]] = {}
        daily: dict[date, dict[str, Any]] = {}

        def _ensure_channel(
            key: str, ca_id: UUID | None, ctype: str, label: str
        ) -> dict[str, Any]:
            if key not in acc:
                acc[key] = {
                    "channel_account_id": ca_id,
                    "channel_type": ctype,
                    "channel_label": label,
                    "order_count": 0,
                    "units_sold": 0,
                    "gmv_base": Decimal("0.00"),
                    "refunded_amount_base": Decimal("0.00"),
                    "cogs_base": Decimal("0.00"),
                    "platform_commission_base": Decimal("0.00"),
                    "ad_spend_base": Decimal("0.00"),
                    "net_margin_base": Decimal("0.00"),
                }
            return acc[key]

        def _ensure_day(day: date) -> dict[str, Any]:
            if day not in daily:
                daily[day] = {
                    "order_count": 0,
                    "units_sold": 0,
                    "gmv_base": Decimal("0.00"),
                    "net_margin_base": Decimal("0.00"),
                    "ad_spend_base": Decimal("0.00"),
                }
            return daily[day]

        for row in order_rows:
            ca_id = row.channel_account_id
            key = str(ca_id) if ca_id is not None else "direct_b2b"
            bucket = _ensure_channel(
                key,
                ca_id,
                str(row.channel_type),
                str(row.channel_label),
            )
            fx = Decimal(str(row.fx_rate_to_base))
            total_amt = Decimal(str(row.total_amount))
            gmv_base = _q2(total_amt * fx)
            refund_ccy = Decimal(str(row.refunded_raw or "0"))
            if str(row.payment_status) == "refunded" and refund_ccy <= Decimal("0"):
                refund_ccy = total_amt
            refund_base = _q2(refund_ccy * fx)

            if row.snap_cogs_base is not None:
                cogs_base = Decimal(str(row.snap_cogs_base))
            elif str(row.fulfillment_status) in {"cancelled", "refunded"}:
                cogs_base = Decimal("0.00")
            else:
                cogs_base = _q2(Decimal(str(row.fallback_cogs_base)))

            comm_base = _q2(
                Decimal(
                    str(
                        row.snap_comm_base
                        if row.snap_comm_base is not None
                        else row.meta_comm_raw or "0"
                    )
                )
            )
            order_ad_base = _q2(
                Decimal(
                    str(
                        row.snap_ad_base
                        if row.snap_ad_base is not None
                        else row.meta_ad_raw or "0"
                    )
                )
            )
            if row.snap_net_margin_base is not None:
                net_margin = Decimal(str(row.snap_net_margin_base))
            elif str(row.fulfillment_status) in {"cancelled", "refunded"}:
                net_margin = _q2(-comm_base - order_ad_base)
            else:
                net_rev = max(Decimal("0.00"), gmv_base - refund_base)
                net_margin = _q2(net_rev - cogs_base - comm_base - order_ad_base)

            units = int(row.units_sold or 0)
            bucket["order_count"] += 1
            bucket["units_sold"] += units
            bucket["gmv_base"] += gmv_base
            bucket["refunded_amount_base"] += refund_base
            bucket["cogs_base"] += cogs_base
            bucket["platform_commission_base"] += comm_base
            bucket["ad_spend_base"] += order_ad_base
            bucket["net_margin_base"] += net_margin

            day_bucket = _ensure_day(row.order_date)
            day_bucket["order_count"] += 1
            day_bucket["units_sold"] += units
            day_bucket["gmv_base"] += gmv_base
            day_bucket["net_margin_base"] += net_margin
            day_bucket["ad_spend_base"] += order_ad_base

        for mrow in metric_rows:
            spend = _q2(Decimal(str(mrow.ad_spend_base or "0")))
            bucket = _ensure_channel(
                str(mrow.channel_account_id),
                mrow.channel_account_id,
                str(mrow.channel_type),
                str(mrow.channel_label),
            )
            bucket["ad_spend_base"] += spend
            bucket["net_margin_base"] -= spend

            day_bucket = _ensure_day(mrow.metric_date)
            day_bucket["ad_spend_base"] += spend
            day_bucket["net_margin_base"] -= spend

        channels = [
            _finalize_breakdown_item(
                channel_key=key,
                channel_account_id=b["channel_account_id"],
                channel_type=b["channel_type"],
                channel_label=b["channel_label"],
                order_count=b["order_count"],
                units_sold=b["units_sold"],
                gmv_base=b["gmv_base"],
                refunded_amount_base=b["refunded_amount_base"],
                cogs_base=b["cogs_base"],
                platform_commission_base=b["platform_commission_base"],
                ad_spend_base=b["ad_spend_base"],
                net_margin_base=b["net_margin_base"],
            )
            for key, b in acc.items()
        ]
        channels.sort(
            key=lambda item: (-item.gmv_base, -item.ad_spend_base, item.channel_label)
        )

        totals = _finalize_breakdown_item(
            channel_key="all",
            channel_account_id=None,
            channel_type="all",
            channel_label="All Channels",
            order_count=sum(c.order_count for c in channels),
            units_sold=sum(c.units_sold for c in channels),
            gmv_base=sum((c.gmv_base for c in channels), start=Decimal("0.00")),
            refunded_amount_base=sum(
                (c.refunded_amount_base for c in channels), start=Decimal("0.00")
            ),
            cogs_base=sum((c.cogs_base for c in channels), start=Decimal("0.00")),
            platform_commission_base=sum(
                (c.platform_commission_base for c in channels), start=Decimal("0.00")
            ),
            ad_spend_base=sum((c.ad_spend_base for c in channels), start=Decimal("0.00")),
            net_margin_base=sum(
                (c.net_margin_base for c in channels), start=Decimal("0.00")
            ),
        )
        trend = [
            ChannelDailyTrendPoint(
                metric_date=day,
                order_count=vals["order_count"],
                units_sold=vals["units_sold"],
                gmv_base=_q2(vals["gmv_base"]),
                net_margin_base=_q2(vals["net_margin_base"]),
                ad_spend_base=_q2(vals["ad_spend_base"]),
            )
            for day, vals in sorted(daily.items(), key=lambda pair: pair[0])
        ]
        return ChannelAnalyticsOverviewRead(
            window=norm_window,
            base_currency=base_currency,
            totals=totals,
            channels=channels,
            daily_trend=trend,
        )

    async def get_sku_velocity(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        *,
        role: UserRole,
        window: str = "30d",
        limit: int = 50,
    ) -> SkuVelocityResponse:
        norm_window, days = parse_analytics_window(window)
        since_ts = datetime.now(UTC) - timedelta(days=days)
        base_currency = await self._tenant_base_currency(session, tenant_id)
        hide_costs = role == UserRole.AGENT

        variant_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        pv.id AS variant_id,
                        pv.product_id,
                        pv.sku,
                        COALESCE(
                            p.i18n_specs->'en'->>'title',
                            p.i18n_specs->'zh'->>'title',
                            p.spu_code
                        ) AS product_name,
                        pv.lead_time_days,
                        pv.cost_price_base,
                        COALESCE(sales.units_sold, 0) AS total_units_sold,
                        COALESCE(bal.available_qty, 0) AS available_qty,
                        sd.id AS sourcing_draft_id,
                        sd.source_url AS sourcing_url
                    FROM product_variants pv
                    JOIN products p
                      ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id
                    LEFT JOIN (
                        SELECT oi.variant_id,
                               COALESCE(SUM(oi.quantity), 0) AS units_sold
                        FROM order_items oi
                        JOIN orders o
                          ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
                        WHERE oi.tenant_id = :tid
                          AND o.created_at >= :since_ts
                          AND o.fulfillment_status <> 'cancelled'
                        GROUP BY oi.variant_id
                    ) sales ON sales.variant_id = pv.id
                    LEFT JOIN (
                        SELECT variant_id,
                               GREATEST(
                                   0, COALESCE(SUM(on_hand_qty - reserved_qty), 0)
                               ) AS available_qty
                        FROM inventory_balances
                        WHERE tenant_id = :tid
                        GROUP BY variant_id
                    ) bal ON bal.variant_id = pv.id
                    LEFT JOIN LATERAL (
                        SELECT id, source_url
                        FROM sourcing_drafts
                        WHERE tenant_id = :tid
                          AND imported_product_id = p.id
                          AND source_url IS NOT NULL
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                    ) sd ON true
                    WHERE pv.tenant_id = :tid
                    ORDER BY total_units_sold DESC, available_qty ASC, pv.sku ASC
                    LIMIT :lim
                    """
                ),
                {"tid": tenant_id, "since_ts": since_ts, "lim": limit},
            )
        ).all()

        variant_ids = [r.variant_id for r in variant_rows]
        breakdown_by_variant: dict[UUID, list[SkuChannelBreakdownItem]] = defaultdict(list)
        if variant_ids:
            ch_rows = (
                await session.execute(
                    text(
                        """
                        SELECT oi.variant_id,
                               COALESCE(ca.channel_type, 'direct_b2b') AS channel_type,
                               COALESCE(ca.display_name, 'Direct B2B') AS channel_label,
                               COALESCE(SUM(oi.quantity), 0) AS units_sold,
                               COALESCE(SUM(oi.line_total * o.fx_rate_to_base), 0) AS revenue_base
                        FROM order_items oi
                        JOIN orders o
                          ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
                        LEFT JOIN channel_accounts ca
                          ON ca.id = o.channel_account_id AND ca.tenant_id = o.tenant_id
                        WHERE oi.tenant_id = :tid
                          AND oi.variant_id = ANY(:vids)
                          AND o.created_at >= :since_ts
                          AND o.fulfillment_status <> 'cancelled'
                        GROUP BY oi.variant_id, COALESCE(ca.channel_type, 'direct_b2b'),
                                 COALESCE(ca.display_name, 'Direct B2B')
                        ORDER BY units_sold DESC
                        """
                    ),
                    {"tid": tenant_id, "vids": variant_ids, "since_ts": since_ts},
                )
            ).all()
            for cr in ch_rows:
                breakdown_by_variant[cr.variant_id].append(
                    SkuChannelBreakdownItem(
                        channel_type=str(cr.channel_type),
                        channel_label=str(cr.channel_label),
                        units_sold=int(cr.units_sold or 0),
                        revenue_base=_q2(Decimal(str(cr.revenue_base or "0"))),
                    )
                )

        items: list[SkuVelocityItem] = []
        for row in variant_rows:
            units = int(row.total_units_sold or 0)
            available = int(row.available_qty or 0)
            lead_time = max(1, int(row.lead_time_days or 7))
            avg_daily = _q2(Decimal(units) / Decimal(days))
            days_of_cover = (
                _q1(Decimal(available) / avg_daily) if avg_daily > Decimal("0") else None
            )
            if available <= 0 and units > 0:
                risk = "out_of_stock"
            elif days_of_cover is not None and days_of_cover < Decimal(lead_time):
                risk = "critical"
            elif days_of_cover is not None and days_of_cover < Decimal(lead_time * 2):
                risk = "warning"
            else:
                risk = "healthy"

            target_units = int(
                (avg_daily * Decimal(lead_time + 14)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            reorder_qty = max(0, target_units - available)
            items.append(
                SkuVelocityItem(
                    variant_id=row.variant_id,
                    product_id=row.product_id,
                    sku=str(row.sku),
                    product_name=str(row.product_name),
                    total_units_sold=units,
                    avg_daily_units=avg_daily,
                    available_qty=available,
                    days_of_cover=days_of_cover,
                    lead_time_days=lead_time,
                    stockout_risk=risk,
                    recommended_reorder_qty=reorder_qty,
                    cost_price_base=(
                        None
                        if hide_costs
                        else _q2(Decimal(str(row.cost_price_base)))
                    ),
                    channel_breakdown=breakdown_by_variant.get(row.variant_id, []),
                    sourcing_url=row.sourcing_url,
                    sourcing_draft_id=row.sourcing_draft_id,
                )
            )

        return SkuVelocityResponse(
            window=norm_window,
            base_currency=base_currency,
            items=items,
            total=len(items),
        )

    async def upsert_daily_metric(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: ChannelDailyMetricUpsert,
    ) -> ChannelDailyMetricRead:
        account_exists = await session.scalar(
            text(
                "SELECT id FROM channel_accounts WHERE tenant_id = :tid AND id = :caid LIMIT 1"
            ),
            {"tid": tenant_id, "caid": payload.channel_account_id},
        )
        if account_exists is None:
            raise AppError(
                slug="channel-account-not-found",
                status_code=404,
                code="ERR_CHANNEL_ACCOUNT_NOT_FOUND",
                title="Channel Account Not Found",
                detail=f"Channel account '{payload.channel_account_id}' not found.",
            )
        if payload.variant_id is not None:
            variant_exists = await session.scalar(
                text(
                    "SELECT id FROM product_variants WHERE tenant_id = :tid AND id = :vid LIMIT 1"
                ),
                {"tid": tenant_id, "vid": payload.variant_id},
            )
            if variant_exists is None:
                raise AppError(
                    slug="variant-not-found",
                    status_code=404,
                    code="ERR_VARIANT_NOT_FOUND",
                    title="Variant Not Found",
                    detail=f"Product variant '{payload.variant_id}' not found.",
                )

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO channel_daily_metrics (
                        tenant_id, channel_account_id, variant_id, metric_date,
                        impressions, clicks, ad_spend_base, external_sessions, source
                    ) VALUES (
                        :tid, :caid, :vid, :mdate,
                        :imp, :clk, :spend, :sess, :src
                    )
                    ON CONFLICT (
                        tenant_id,
                        channel_account_id,
                        COALESCE(variant_id, '00000000-0000-0000-0000-000000000000'::uuid),
                        metric_date
                    )
                    DO UPDATE SET
                        impressions = EXCLUDED.impressions,
                        clicks = EXCLUDED.clicks,
                        ad_spend_base = EXCLUDED.ad_spend_base,
                        external_sessions = EXCLUDED.external_sessions,
                        source = EXCLUDED.source,
                        updated_at = now()
                    RETURNING *
                    """
                ),
                {
                    "tid": tenant_id,
                    "caid": payload.channel_account_id,
                    "vid": payload.variant_id,
                    "mdate": payload.metric_date,
                    "imp": payload.impressions,
                    "clk": payload.clicks,
                    "spend": _q2(payload.ad_spend_base),
                    "sess": payload.external_sessions,
                    "src": payload.source.strip(),
                },
            )
        ).one()
        return ChannelDailyMetricRead(
            id=row.id,
            tenant_id=row.tenant_id,
            channel_account_id=row.channel_account_id,
            variant_id=row.variant_id,
            metric_date=row.metric_date,
            impressions=int(row.impressions),
            clicks=int(row.clicks),
            ad_spend_base=Decimal(str(row.ad_spend_base)),
            external_sessions=int(row.external_sessions),
            source=str(row.source),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


__all__ = ["ChannelAnalyticsService", "parse_analytics_window"]
