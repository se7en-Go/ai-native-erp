"""Unified cross-channel sales analytics view, marketplace buyer identity, and IM sourcing copies.

Three deliverables:

1. `ck_customer_identities_type` admits the eleventh identity type
   `marketplace_buyer_id`, so a marketplace-local buyer handle (Shopee / Lazada /
   SHEIN / AliExpress / Ozon buyer id) can be merged onto one human customer.
2. `sourcing_drafts` gains `localized_copies` (per-locale listing copy decks) and
   `marketplace_category_attrs` (per-marketplace category attribute payloads).
3. `vw_tenant_channel_sales_analytics` unifies every channel - marketplace, social,
   email, and direct B2B orders with `channel_account_id IS NULL` - into one daily
   grain of GMV, refunds, COGS, platform commission, ad spend, net contribution
   margin, impressions, clicks, and external sessions. The view is
   `security_invoker = true`, so the FORCE RLS policies of `orders`,
   `order_profit_snapshots`, `channel_daily_metrics`, and `channel_accounts` decide
   visibility for `app_runtime`; the view itself never bypasses tenant isolation.

Revision ID: 0019_analytics_im_sourcing
Revises: 0018_marketplace_channels
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_analytics_im_sourcing"
down_revision: str | None = "0018_marketplace_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Ten identity types admitted by `0002_channels_identity_inbox`.
_LEGACY_IDENTITY_TYPES: tuple[str, ...] = (
    "email",
    "phone_e164",
    "whatsapp_waid",
    "whatsapp_bsuid",
    "ig_psid",
    "fb_psid",
    "shopify_customer_id",
    "tiktok_buyer_id",
    "amazon_buyer_email",
    "web_visitor_id",
)
#: Marketplace-local buyer handle merged onto the same customer graph.
_MARKETPLACE_IDENTITY_TYPES: tuple[str, ...] = ("marketplace_buyer_id",)
_IDENTITY_TYPES: tuple[str, ...] = (*_LEGACY_IDENTITY_TYPES, *_MARKETPLACE_IDENTITY_TYPES)

_IDENTITY_CONSTRAINT = "ck_customer_identities_type"

#: Text shape accepted before a JSONB field is cast to numeric (defensive parse).
_NUMERIC_TEXT_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"


def _identity_check(identity_types: tuple[str, ...]) -> str:
    literals = ", ".join(f"'{identity_type}'" for identity_type in identity_types)
    return f"identity_type IN ({literals})"


def _jsonb_numeric(metadata_key: str) -> str:
    """Defensive `channel_metadata` numeric cast: non-numeric text becomes 0."""
    expr = f"o.channel_metadata->>'{metadata_key}'"
    return (
        "CASE\n"
        f"                            WHEN coalesce({expr}, '') "
        f"~ '{_NUMERIC_TEXT_PATTERN}'\n"
        f"                                THEN ({expr})::numeric\n"
        "                            ELSE 0\n"
        "                        END"
    )


def _refunded_amount() -> str:
    """Native-currency refunded amount, preferring the explicit webhook field."""
    return (
        "CASE\n"
        "                            WHEN coalesce("
        "o.channel_metadata->>'refunded_amount', '') "
        f"~ '{_NUMERIC_TEXT_PATTERN}'\n"
        "                                THEN (o.channel_metadata->>'refunded_amount')::numeric\n"
        "                            WHEN o.payment_status = 'refunded' THEN o.total_amount\n"
        "                            ELSE 0\n"
        "                        END"
    )


def _analytics_view_sql() -> str:
    """Daily per-channel sales analytics view across every channel type."""
    refunded = _refunded_amount()
    commission = _jsonb_numeric("platform_commission_base")
    ad_cost = _jsonb_numeric("ad_attribution_cost_base")
    return f"""
        CREATE OR REPLACE VIEW vw_tenant_channel_sales_analytics
        WITH (security_invoker = true) AS
        WITH order_daily AS (
            SELECT
                per_order.tenant_id,
                per_order.channel_account_id,
                per_order.metric_date,
                COUNT(*)::bigint AS order_count,
                COALESCE(ROUND(SUM(per_order.gmv_base), 2), 0) AS gmv_base,
                COALESCE(ROUND(SUM(per_order.refunded_amount_base), 2), 0)
                    AS refunded_amount_base,
                COALESCE(ROUND(SUM(per_order.cogs_base), 2), 0) AS cogs_base,
                COALESCE(ROUND(SUM(per_order.platform_commission_base), 2), 0)
                    AS platform_commission_base,
                COALESCE(ROUND(SUM(per_order.order_ad_spend_base), 2), 0)
                    AS order_ad_spend_base,
                COALESCE(ROUND(SUM(per_order.order_net_margin_base), 2), 0)
                    AS order_net_margin_base
            FROM (
                SELECT
                    o.tenant_id,
                    o.channel_account_id,
                    (o.created_at AT TIME ZONE 'UTC')::date AS metric_date,
                    COALESCE(ROUND(o.total_amount * o.fx_rate_to_base, 2), 0) AS gmv_base,
                    ROUND({refunded} * o.fx_rate_to_base, 2) AS refunded_amount_base,
                    COALESCE(ops.cogs_base, 0) AS cogs_base,
                    COALESCE(ops.platform_commission_base, {commission}, 0)
                        AS platform_commission_base,
                    COALESCE(ops.ad_attribution_cost_base, {ad_cost}, 0)
                        AS order_ad_spend_base,
                    COALESCE(
                        ops.net_contribution_margin_base,
                        ROUND(
                            GREATEST(
                                0,
                                o.total_amount * o.fx_rate_to_base
                                    - {refunded} * o.fx_rate_to_base
                            )
                            - COALESCE(ops.cogs_base, 0)
                            - COALESCE(ops.platform_commission_base, {commission}, 0)
                            - COALESCE(ops.ad_attribution_cost_base, {ad_cost}, 0),
                            2
                        )
                    ) AS order_net_margin_base
                FROM orders o
                LEFT JOIN order_profit_snapshots ops
                    ON ops.order_id = o.id AND ops.tenant_id = o.tenant_id
            ) per_order
            GROUP BY
                per_order.tenant_id,
                per_order.channel_account_id,
                per_order.metric_date
        ),
        traffic_daily AS (
            SELECT
                cdm.tenant_id,
                cdm.channel_account_id,
                cdm.metric_date,
                COALESCE(SUM(cdm.ad_spend_base), 0) AS traffic_ad_spend_base,
                COALESCE(SUM(cdm.impressions), 0) AS impressions,
                COALESCE(SUM(cdm.clicks), 0) AS clicks,
                COALESCE(SUM(cdm.external_sessions), 0) AS external_sessions
            FROM channel_daily_metrics cdm
            GROUP BY cdm.tenant_id, cdm.channel_account_id, cdm.metric_date
        )
        SELECT
            COALESCE(od.tenant_id, td.tenant_id) AS tenant_id,
            COALESCE(od.channel_account_id, td.channel_account_id) AS channel_account_id,
            COALESCE(ca.channel_type, 'direct_b2b') AS channel_type,
            COALESCE(ca.display_name, 'Direct B2B') AS channel_label,
            COALESCE(od.metric_date, td.metric_date) AS metric_date,
            COALESCE(od.order_count, 0)::bigint AS order_count,
            COALESCE(od.gmv_base, 0)::numeric(14,2) AS gmv_base,
            COALESCE(od.refunded_amount_base, 0)::numeric(14,2) AS refunded_amount_base,
            COALESCE(od.cogs_base, 0)::numeric(14,2) AS cogs_base,
            COALESCE(od.platform_commission_base, 0)::numeric(14,2)
                AS platform_commission_base,
            (COALESCE(od.order_ad_spend_base, 0) + COALESCE(td.traffic_ad_spend_base, 0))
                ::numeric(14,2) AS ad_spend_base,
            (COALESCE(od.order_net_margin_base, 0) - COALESCE(td.traffic_ad_spend_base, 0))
                ::numeric(14,2) AS net_margin_base,
            COALESCE(td.impressions, 0)::bigint AS impressions,
            COALESCE(td.clicks, 0)::bigint AS clicks,
            COALESCE(td.external_sessions, 0)::bigint AS external_sessions
        FROM order_daily od
        FULL OUTER JOIN traffic_daily td
            ON od.tenant_id = td.tenant_id
            AND od.channel_account_id IS NOT DISTINCT FROM td.channel_account_id
            AND od.metric_date = td.metric_date
        LEFT JOIN channel_accounts ca
            ON ca.id = COALESCE(od.channel_account_id, td.channel_account_id)
            AND ca.tenant_id = COALESCE(od.tenant_id, td.tenant_id)
        """


def upgrade() -> None:
    # (a) Admit the marketplace-local buyer handle on the identity allowlist.
    op.drop_constraint(_IDENTITY_CONSTRAINT, "customer_identities", type_="check")
    op.create_check_constraint(
        _IDENTITY_CONSTRAINT, "customer_identities", _identity_check(_IDENTITY_TYPES)
    )

    # (b) Localized listing copy decks and per-marketplace category attribute payloads.
    op.execute("""
        ALTER TABLE sourcing_drafts
        ADD COLUMN IF NOT EXISTS localized_copies JSONB NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN IF NOT EXISTS marketplace_category_attrs JSONB NOT NULL DEFAULT '{}'::jsonb
    """)

    # (c) Cross-channel daily sales analytics (RLS preserved through security_invoker).
    op.execute(_analytics_view_sql())
    op.execute("GRANT SELECT ON vw_tenant_channel_sales_analytics TO app_runtime")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_tenant_channel_sales_analytics")
    op.execute("""
        ALTER TABLE sourcing_drafts
        DROP COLUMN IF EXISTS marketplace_category_attrs,
        DROP COLUMN IF EXISTS localized_copies
    """)
    # A surviving `marketplace_buyer_id` row makes the narrowed CHECK fail validation,
    # which is the intended lossy-downgrade guard.
    op.drop_constraint(_IDENTITY_CONSTRAINT, "customer_identities", type_="check")
    op.create_check_constraint(
        _IDENTITY_CONSTRAINT, "customer_identities", _identity_check(_LEGACY_IDENTITY_TYPES)
    )
