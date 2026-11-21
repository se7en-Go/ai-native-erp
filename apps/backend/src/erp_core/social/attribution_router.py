"""`/api/v1/social` attribution funnel: social posts -> mentions -> CRM -> quotes.

Every number here comes from deterministic, tenant-scoped SQL over the real
tables (`social_posts`, `social_mentions`, `leads`, `opportunities`, `quotes`).
This module lives in `erp_core.social`, so per `infra/importlinter.ini`
(`social_isolated`) it may **not** import `erp_core.ai_engine`,
`erp_core.orders`, `erp_core.finance` or `erp_core.billing`, and it imports no
CRM ORM model either: `leads` / `opportunities` / `quotes` are reached through
parameterised SQL scoped by `tenant_id`, with RLS as the second line of defence.

Note on table names: the locked Phase-3A schema names the B2B quote table
`public.quotes` (it hangs off `opportunities` -> `leads`); there is no
`b2b_quotes` relation in this database, so the funnel counts `quotes`.

Attribution rule (deterministic, no probabilistic modelling):

* a lead's social platform is `social_mentions.platform` when the lead was
  captured from a mention (`social_mentions.converted_lead_id = leads.id`),
  otherwise the lead's own `source_channel` (e.g. `instagram`, `tiktok`);
* a lead counts as social when that resolved platform is one of the 14 social
  platforms or starts with `social` (e.g. `social_mention`);
* `social_leads_captured` is the number of those social leads (mention-derived
  *and* source-channel attributed), so the conversion rate below can never
exceed 100%;
* a lead counts as converted when `leads.status = 'converted'`, and the funnel
  counts distinct `leads.customer_id` so one buyer is never double-counted;
* `lead_to_customer_rate_pct = converted_crm_customers / social_leads_captured`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal, get_tenant_db

router = APIRouter(prefix="/api/v1/social", tags=["social-attribution"])

#: The locked 14-platform social matrix (`ck_social_posts_platform`).
SOCIAL_PLATFORMS: tuple[str, ...] = (
    "instagram",
    "tiktok",
    "facebook",
    "youtube",
    "linkedin",
    "pinterest",
    "x",
    "xiaohongshu",
    "douyin",
    "kuaishou",
    "bilibili",
    "wechat_video",
    "wechat_oa",
    "weibo",
)

_PLATFORM_LITERALS = ", ".join(f"'{platform}'" for platform in SOCIAL_PLATFORMS)
# Resolution order: the mention platform wins, else the lead's own source channel.
_SOCIAL_SOURCE = "lower(COALESCE(m.platform, l.source_channel))"
_SOCIAL_MATCH = (
    f"({_SOCIAL_SOURCE} IN ({_PLATFORM_LITERALS}) OR {_SOCIAL_SOURCE} LIKE 'social%')"
)
# Same predicate once the CTE has already resolved the platform into a column.
_RESOLVED_MATCH = f"(platform IN ({_PLATFORM_LITERALS}) OR platform LIKE 'social%')"

_POSTS_SQL = """
    SELECT
        platform,
        COUNT(*)                                     AS total_posts,
        COUNT(*) FILTER (WHERE status = 'published') AS published_posts
    FROM social_posts
    WHERE tenant_id = :tid
    GROUP BY platform
"""

# Listening coverage: a platform with mentions but no post/lead still shows up in the
# breakdown (with zeroes), so an empty funnel row is never silently hidden.
_MENTION_PLATFORMS_SQL = """
    SELECT DISTINCT platform
    FROM social_mentions
    WHERE tenant_id = :tid
"""

_SOCIAL_LEADS_CTE = f"""
    WITH social_lead AS (
        SELECT
            l.id             AS lead_id,
            l.customer_id    AS customer_id,
            l.status         AS status,
            {_SOCIAL_SOURCE} AS platform
        FROM leads l
        LEFT JOIN social_mentions m
            ON m.tenant_id = l.tenant_id
           AND m.converted_lead_id = l.id
        WHERE l.tenant_id = :tid
    )
"""

_CAPTURED_BY_PLATFORM_SQL = (
    _SOCIAL_LEADS_CTE
    + f"""
    SELECT platform, COUNT(DISTINCT lead_id) AS leads_captured
    FROM social_lead
    WHERE {_RESOLVED_MATCH}
    GROUP BY platform
"""
)

_CONVERTED_BY_PLATFORM_SQL = (
    _SOCIAL_LEADS_CTE
    + f"""
    SELECT platform, COUNT(DISTINCT customer_id) AS converted_customers
    FROM social_lead
    WHERE status = 'converted'
      AND {_RESOLVED_MATCH}
    GROUP BY platform
"""
)

_CONVERTED_TOTAL_SQL = (
    _SOCIAL_LEADS_CTE
    + f"""
    SELECT COUNT(DISTINCT customer_id) AS converted_customers
    FROM social_lead
    WHERE status = 'converted'
      AND {_RESOLVED_MATCH}
"""
)

_LINKED_QUOTES_SQL = f"""
    SELECT COUNT(DISTINCT q.id) AS linked_quotes
    FROM quotes q
    JOIN opportunities o
        ON o.id = q.opportunity_id AND o.tenant_id = q.tenant_id
    JOIN leads l
        ON l.id = o.lead_id AND l.tenant_id = o.tenant_id
    LEFT JOIN social_mentions m
        ON m.tenant_id = l.tenant_id AND m.converted_lead_id = l.id
    WHERE q.tenant_id = :tid
      AND {_SOCIAL_MATCH}
"""


def _pct(numerator: int, denominator: int) -> Decimal:
    """Percentage with 2 dp, `ROUND_HALF_UP`; an empty funnel is exactly 0.00."""
    if denominator <= 0:
        return Decimal("0.00")
    return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


class SocialPlatformFunnelItem(BaseModel):
    """One platform's slice of the funnel."""

    platform: str
    total_posts: int
    published_posts: int
    social_leads_captured: int
    converted_crm_customers: int
    lead_to_customer_rate_pct: Decimal


class SocialAttributionFunnelRead(BaseModel):
    """Social -> CRM -> quote attribution for the whole tenant."""

    published_posts: int
    total_posts: int
    social_leads_captured: int
    converted_crm_customers: int
    linked_quotes_count: int
    lead_to_customer_rate_pct: Decimal
    platform_breakdown: list[SocialPlatformFunnelItem]


async def compute_attribution_funnel(
    session: AsyncSession, tenant_id: UUID
) -> SocialAttributionFunnelRead:
    """Read the funnel from real SQL; nothing is cached, mocked or estimated."""
    post_rows = (await session.execute(text(_POSTS_SQL), {"tid": tenant_id})).mappings().all()
    mention_platforms = {
        str(platform)
        for platform in (
            await session.execute(text(_MENTION_PLATFORMS_SQL), {"tid": tenant_id})
        ).scalars()
    }
    captured_rows = (
        (await session.execute(text(_CAPTURED_BY_PLATFORM_SQL), {"tid": tenant_id}))
        .mappings()
        .all()
    )
    converted_rows = (
        (await session.execute(text(_CONVERTED_BY_PLATFORM_SQL), {"tid": tenant_id}))
        .mappings()
        .all()
    )
    converted_total = int(
        (await session.execute(text(_CONVERTED_TOTAL_SQL), {"tid": tenant_id})).scalar_one()
        or 0
    )
    linked_quotes = int(
        (await session.execute(text(_LINKED_QUOTES_SQL), {"tid": tenant_id})).scalar_one() or 0
    )

    total_by_platform = {str(row["platform"]): int(row["total_posts"]) for row in post_rows}
    published_by_platform = {
        str(row["platform"]): int(row["published_posts"]) for row in post_rows
    }
    captured_by_platform = {
        str(row["platform"]): int(row["leads_captured"]) for row in captured_rows
    }
    converted_by_platform = {
        str(row["platform"]): int(row["converted_customers"]) for row in converted_rows
    }

    breakdown: list[SocialPlatformFunnelItem] = []
    for platform in sorted(
        set(total_by_platform)
        | set(captured_by_platform)
        | set(converted_by_platform)
        | mention_platforms
    ):
        captured = captured_by_platform.get(platform, 0)
        converted = converted_by_platform.get(platform, 0)
        breakdown.append(
            SocialPlatformFunnelItem(
                platform=platform,
                total_posts=total_by_platform.get(platform, 0),
                published_posts=published_by_platform.get(platform, 0),
                social_leads_captured=captured,
                converted_crm_customers=converted,
                lead_to_customer_rate_pct=_pct(converted, captured),
            )
        )

    captured_total = sum(item.social_leads_captured for item in breakdown)
    return SocialAttributionFunnelRead(
        published_posts=sum(item.published_posts for item in breakdown),
        total_posts=sum(item.total_posts for item in breakdown),
        social_leads_captured=captured_total,
        converted_crm_customers=converted_total,
        linked_quotes_count=linked_quotes,
        lead_to_customer_rate_pct=_pct(converted_total, captured_total),
        platform_breakdown=breakdown,
    )


TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
AnyPrincipal = Annotated[CurrentPrincipal, Depends(get_current_principal)]


@router.get("/attribution-funnel", response_model=SocialAttributionFunnelRead)
async def get_attribution_funnel(
    session: TenantDB,
    principal: AnyPrincipal,
) -> SocialAttributionFunnelRead:
    """Read-only funnel, open to every authenticated role of the tenant."""
    return await compute_attribution_funnel(session, principal.tenant_id)
