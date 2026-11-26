"""`/api/v1/finance/ads` - read-only ad spend connection and reporting.

The router owns the fully-qualified prefix (already admitted by the operator BFF
allowlist via the `finance` prefix), so the composition root only needs
``include_router(ads_router)``.

RBAC: `OWNER`/`ADMIN`/`MANAGER`/`FINANCE` may read accounts and the spend
summary (ad spend is a finance number, so `AGENT` is refused on every route -
reads included). Connecting, syncing and deleting an account requires
`OWNER`/`ADMIN`/`MANAGER`; `FINANCE` reviews spend but does not wire up
credentials.

Every write is a *pull*: `POST /accounts/{id}/sync` performs HTTPS `GET`
requests against the ad platform and upserts local snapshots. Nothing is ever
pushed back to Meta or Amazon.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.ads_schemas import (
    AdAccountConnect,
    AdAccountRead,
    AdsSpendSummaryRead,
    AdsSyncResultRead,
)
from erp_core.finance.ads_service import (
    DEFAULT_ADS_SYNC_DAYS,
    MAX_ADS_DAYS,
    AdsSpendService,
)
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/finance/ads", tags=["finance-ads"])

#: Roles allowed to read ad accounts and spend analytics.
ADS_READ_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.FINANCE,
)
#: Roles allowed to connect, sync and delete an ad account.
ADS_WRITE_ROLES: tuple[UserRole, ...] = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
)

TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
AdsReader = Annotated[CurrentPrincipal, Depends(require_roles(*ADS_READ_ROLES))]
AdsWriter = Annotated[CurrentPrincipal, Depends(require_roles(*ADS_WRITE_ROLES))]


def _service(request: Request) -> AdsSpendService:
    """Use the composition root's adapter registry when it injected one.

    Tests replace `app.state.ads_adapters` with adapters carrying an
    `httpx.MockTransport`, which is how the Meta and Amazon adapters are
    exercised without a network call.
    """
    adapters = getattr(request.app.state, "ads_adapters", None)
    return AdsSpendService(adapters=adapters)


def _crypto(request: Request) -> EnvelopeCryptoService:
    crypto = getattr(request.app.state, "crypto_service", None)
    if not isinstance(crypto, EnvelopeCryptoService):
        raise AppError(
            slug="crypto-service-unavailable",
            title="Encryption Service Unavailable",
            status_code=500,
            detail="The envelope encryption service is not initialised on the app.",
            code="ERR_CRYPTO_UNAVAILABLE",
        )
    return crypto


@router.get("/accounts", response_model=list[AdAccountRead])
async def list_ad_accounts(
    request: Request,
    principal: AdsReader,
    session: TenantDB,
) -> list[AdAccountRead]:
    """Connected ad accounts. Tokens are never echoed - only a masked fingerprint."""
    return await _service(request).list_accounts(session, principal.tenant_id)


@router.post("/accounts", response_model=AdAccountRead, status_code=201)
async def connect_ad_account(
    request: Request,
    payload: AdAccountConnect,
    principal: AdsWriter,
    session: TenantDB,
) -> AdAccountRead:
    """Connect a `sandbox_ads` / `meta_ads` / `amazon_ads` account read-only."""
    return await _service(request).connect_account(
        session,
        _crypto(request),
        principal.tenant_id,
        payload,
    )


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_ad_account(
    request: Request,
    account_id: UUID,
    principal: AdsWriter,
    session: TenantDB,
) -> None:
    """Disconnect the account; its daily snapshots cascade away with it."""
    await _service(request).delete_account(session, principal.tenant_id, account_id)


@router.post("/accounts/{account_id}/sync", response_model=AdsSyncResultRead)
async def sync_ad_account(
    request: Request,
    account_id: UUID,
    principal: AdsWriter,
    session: TenantDB,
    days: int = Query(default=DEFAULT_ADS_SYNC_DAYS, ge=1, le=MAX_ADS_DAYS),
) -> AdsSyncResultRead:
    """Pull the last `days` of daily campaign spend (read-only HTTPS GET) and upsert."""
    return await _service(request).sync_account(
        session,
        _crypto(request),
        principal.tenant_id,
        account_id,
        days=days,
    )


@router.get("/spend-summary", response_model=AdsSpendSummaryRead)
async def get_ads_spend_summary(
    request: Request,
    principal: AdsReader,
    session: TenantDB,
    days: int = Query(default=30, ge=1, le=MAX_ADS_DAYS),
) -> AdsSpendSummaryRead:
    """Totals plus exact ROAS / TACoS / CTR as decimal strings."""
    return await _service(request).spend_summary(
        session, principal.tenant_id, days=days
    )
