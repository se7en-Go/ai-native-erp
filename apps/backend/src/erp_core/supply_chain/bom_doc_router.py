"""`/api/v1/supply-chain` Track 7 routes: light BOM, headhaul transfers, DOC.

Reads are open to every authenticated member of the tenant (owner / admin /
manager / agent / finance) because stock, in-transit and days-of-cover are
operational facts the whole team needs. Every write - assembling a kit,
dispatching or receiving a transfer, editing a BOM - is restricted to the
stock-owning roles (owner / admin / manager), matching `/wms` stocktakes.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import UserRole
from erp_core.supply_chain import bom_doc_service
from erp_core.supply_chain.bom_doc_schemas import (
    BomAssembleRead,
    BomAssembleRequest,
    BomComponentRead,
    BomComponentUpsert,
    BomCostRollupRead,
    BomListRead,
    DocReplenishmentRead,
    StockTransferCreate,
    StockTransferListRead,
    StockTransferRead,
)
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/supply-chain", tags=["supply-chain"])

_MANAGERS = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
Operator = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGERS))]
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db, scope="function")]
TargetDocDays = Annotated[int, Query(ge=1, le=365)]


# ---------------------------------------------------------------------------
# 1-level light BOM
# ---------------------------------------------------------------------------


@router.get("/bom")
async def list_bom(
    principal: Reader,
    session: TenantDB,
    parent_variant_id: UUID | None = None,
) -> BomListRead:
    """Every 1-level kit (optionally just one parent) with its components and cost."""
    return await bom_doc_service.list_bom_kits(session, principal.tenant_id, parent_variant_id)


@router.post("/bom/components", status_code=201)
async def upsert_bom_component(
    principal: Operator,
    session: TenantDB,
    payload: BomComponentUpsert,
) -> BomComponentRead:
    """Add or update a component; rejects self-reference and any 2nd BOM level (422)."""
    return await bom_doc_service.upsert_component(session, principal.tenant_id, payload)


@router.delete("/bom/components/{component_id}", status_code=204)
async def delete_bom_component(
    principal: Operator,
    session: TenantDB,
    component_id: UUID,
) -> Response:
    await bom_doc_service.delete_component(session, principal.tenant_id, component_id)
    return Response(status_code=204)


@router.get("/bom/{parent_variant_id}/cost-rollup")
async def read_bom_cost_rollup(
    principal: Reader,
    session: TenantDB,
    parent_variant_id: UUID,
) -> BomCostRollupRead:
    """Exact `Decimal` rolled-up unit cost from each component's cost price."""
    return await bom_doc_service.bom_cost_rollup(session, principal.tenant_id, parent_variant_id)


@router.post("/bom/{parent_variant_id}/assemble", status_code=201)
async def assemble_bom(
    principal: Operator,
    session: TenantDB,
    parent_variant_id: UUID,
    payload: BomAssembleRequest,
) -> BomAssembleRead:
    """Deduct every component and credit the kit, atomically and idempotently."""
    return await bom_doc_service.assemble_kit(
        session,
        principal.tenant_id,
        principal.user_id,
        parent_variant_id,
        payload.warehouse_id,
        payload.qty_kits,
        payload.request_id,
        payload.notes,
    )


# ---------------------------------------------------------------------------
# In-transit headhaul stock transfers
# ---------------------------------------------------------------------------


@router.get("/transfers")
async def list_stock_transfers(
    principal: Reader,
    session: TenantDB,
) -> StockTransferListRead:
    """Tenant transfers with lines, SKUs and warehouse names."""
    return await bom_doc_service.list_transfers(session, principal.tenant_id)


@router.post("/transfers", status_code=201)
async def create_stock_transfer(
    principal: Operator,
    session: TenantDB,
    payload: StockTransferCreate,
) -> StockTransferRead:
    return await bom_doc_service.create_transfer(
        session, principal.tenant_id, principal.user_id, payload
    )


@router.post("/transfers/{transfer_id}/dispatch")
async def dispatch_stock_transfer(
    principal: Operator,
    session: TenantDB,
    transfer_id: UUID,
) -> StockTransferRead:
    """`draft -> in_transit`: deduct the source warehouse, stamp `dispatched_at`."""
    return await bom_doc_service.dispatch_transfer(
        session, principal.tenant_id, principal.user_id, transfer_id
    )


@router.post("/transfers/{transfer_id}/receive")
async def receive_stock_transfer(
    principal: Operator,
    session: TenantDB,
    transfer_id: UUID,
) -> StockTransferRead:
    """`in_transit -> received`: credit the destination warehouse."""
    return await bom_doc_service.receive_transfer(
        session, principal.tenant_id, principal.user_id, transfer_id
    )


@router.post("/transfers/{transfer_id}/cancel")
async def cancel_stock_transfer(
    principal: Operator,
    session: TenantDB,
    transfer_id: UUID,
) -> StockTransferRead:
    """Cancel a draft transfer; a dispatched or received load cannot be cancelled."""
    return await bom_doc_service.cancel_transfer(session, principal.tenant_id, transfer_id)


# ---------------------------------------------------------------------------
# Days-of-cover (DOC) weighted replenishment
# ---------------------------------------------------------------------------


@router.get("/doc-replenishment")
async def read_doc_replenishment(
    principal: Reader,
    session: TenantDB,
    target_doc_days: TargetDocDays = 30,
) -> DocReplenishmentRead:
    """Per-SKU local / overseas / in-transit stock, blended daily sales and DOC."""
    return await bom_doc_service.doc_replenishment(
        session, principal.tenant_id, target_doc_days
    )
