"""`/api/v1/finance` AP routes: supplier payables, L0 settlement and cash flow.

The router owns the fully-qualified ``/api/v1/finance`` prefix so the
composition root can simply ``include_router(ap_router)``; `finance/router.py`
keeps its legacy ``/finance`` prefix plus the ``/api/v1`` mount, and both resolve
to the same public paths. Every handler runs inside `get_tenant_db`, i.e. a
transaction-scoped session with ``app.current_tenant_id`` set, so the
``RESTRICTIVE`` RLS policies stay authoritative.

RBAC: reads allow Owner/Admin/Manager/Finance, paying a supplier (an L0 fund
red line) allows only Owner/Finance.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.ap_service import (
    AP_PAY_ROLES,
    AP_READ_ROLES,
    CashflowSummaryRead,
    PayableListRead,
    PayablePaymentRequest,
    PayableRead,
    PayableService,
)
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_tenant_db,
    require_roles,
)

router = APIRouter(prefix="/api/v1/finance", tags=["finance-ap"])

_service = PayableService()

TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
ApReader = Annotated[CurrentPrincipal, Depends(require_roles(*AP_READ_ROLES))]
ApPayer = Annotated[CurrentPrincipal, Depends(require_roles(*AP_PAY_ROLES))]


@router.get("/payables", response_model=PayableListRead)
async def list_payables(
    session: TenantDB,
    principal: ApReader,
) -> PayableListRead:
    """Auto-book missing POs as `open` AP documents, then list the whole ledger."""
    return await _service.list_payables(session, principal.tenant_id, principal)


@router.post("/payables/{payable_id}/pay", response_model=PayableRead)
async def pay_payable(
    payable_id: UUID,
    payload: PayablePaymentRequest,
    session: TenantDB,
    principal: ApPayer,
) -> PayableRead:
    """Settle a payable; `open -> partial -> paid`, audited, never overpaid."""
    return await _service.record_payment(
        session, principal.tenant_id, principal, payable_id, payload
    )


@router.get("/cashflow-summary", response_model=CashflowSummaryRead)
async def get_cashflow_summary(
    session: TenantDB,
    principal: ApReader,
) -> CashflowSummaryRead:
    """AR collected minus AP paid, with outstanding balances, in the base currency."""
    return await _service.cashflow_summary(session, principal.tenant_id)
