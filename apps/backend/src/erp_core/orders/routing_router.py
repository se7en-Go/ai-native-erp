"""HTTP surface for order routing rules, anti-loss holds, and carrier waybills.

Mounted under `/api/v1/orders` - the `/orders` namespace is already allow-listed by the
operator BFF, so the Next.js shell never needs a proxy change. Every route is
tenant-scoped through `tenant_session` (PostgreSQL FORCE RLS) and role-gated at the
router boundary *and* inside the services.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.orders.carrier_label_service import (
    book_shipping_label,
    get_label_pdf_bytes,
)
from erp_core.orders.routing_models import CarrierShippingLabel
from erp_core.orders.routing_schemas import (
    CarrierShippingLabelRead,
    OrderRoutingEvaluationRead,
    ReleaseHoldRequest,
    RoutingRuleCreate,
    RoutingRuleRead,
    RoutingRuleUpdate,
    ShippingLabelBookRequest,
)
from erp_core.orders.routing_service import RoutingService
from erp_core.orders.tracking_adapter import Tracking17TrackAdapter
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/orders", tags=["order-routing"])
_routing = RoutingService()

Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
Manager = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
]
Shipper = Annotated[
    CurrentPrincipal,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.MANAGER,
            UserRole.FINANCE,
        )
    ),
]


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _storage(request: Request) -> ObjectStorageService | None:
    return getattr(request.app.state, "storage", None)


def _tracking_adapter(request: Request) -> Tracking17TrackAdapter | None:
    adapter = getattr(request.app.state, "tracking_adapter", None)
    if isinstance(adapter, Tracking17TrackAdapter):
        return adapter
    return None


async def _latest_evaluation(
    request: Request, principal: CurrentPrincipal, order_id: UUID
) -> OrderRoutingEvaluationRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        evaluation = await _routing.latest_evaluation(
            session, principal.tenant_id, order_id
        )
    if evaluation is None:
        raise AppError(
            slug="routing-evaluation-not-found",
            status_code=404,
            code="ERR_ROUTING_EVALUATION_NOT_FOUND",
            title="Routing Evaluation Not Found",
            detail=(
                "This order has not been routed yet. Run an evaluation before reading it."
            ),
        )
    return OrderRoutingEvaluationRead.model_validate(evaluation)


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------


@router.get("/routing-rules", response_model=list[RoutingRuleRead])
async def list_routing_rules(
    request: Request, principal: Reader
) -> list[RoutingRuleRead]:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await _routing.list_rules(session, principal.tenant_id)


@router.post(
    "/routing-rules",
    response_model=RoutingRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_routing_rule(
    request: Request, principal: Manager, payload: RoutingRuleCreate
) -> RoutingRuleRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await _routing.create_rule(
            session, principal.tenant_id, principal, payload
        )


@router.patch("/routing-rules/{rule_id}", response_model=RoutingRuleRead)
async def update_routing_rule(
    request: Request,
    principal: Manager,
    rule_id: UUID,
    payload: RoutingRuleUpdate,
) -> RoutingRuleRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await _routing.update_rule(
            session, principal.tenant_id, principal, rule_id, payload
        )


@router.delete("/routing-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_routing_rule(
    request: Request, principal: Manager, rule_id: UUID
) -> Response:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        await _routing.delete_rule(session, principal.tenant_id, principal, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Evaluation + anti-loss hold
# ---------------------------------------------------------------------------


@router.get(
    "/{order_id}/routing-evaluation",
    response_model=OrderRoutingEvaluationRead,
)
async def get_routing_evaluation(
    request: Request, principal: Reader, order_id: UUID
) -> OrderRoutingEvaluationRead:
    return await _latest_evaluation(request, principal, order_id)


@router.post(
    "/{order_id}/evaluate-routing",
    response_model=OrderRoutingEvaluationRead,
    status_code=status.HTTP_201_CREATED,
)
async def evaluate_order_routing(
    request: Request, principal: Reader, order_id: UUID
) -> OrderRoutingEvaluationRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        evaluation = await _routing.evaluate_order(session, principal.tenant_id, order_id)
    return OrderRoutingEvaluationRead.model_validate(evaluation)


@router.post(
    "/{order_id}/release-hold",
    response_model=OrderRoutingEvaluationRead,
)
async def release_routing_hold(
    request: Request,
    principal: Manager,
    order_id: UUID,
    payload: ReleaseHoldRequest | None = None,
) -> OrderRoutingEvaluationRead:
    reason = payload.reason if payload is not None else ""
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        evaluation = await _routing.release_hold(
            session, principal.tenant_id, principal, order_id, reason
        )
    return OrderRoutingEvaluationRead.model_validate(evaluation)


# ---------------------------------------------------------------------------
# Carrier waybills
# ---------------------------------------------------------------------------


@router.get(
    "/{order_id}/shipping-labels", response_model=list[CarrierShippingLabelRead]
)
async def list_shipping_labels(
    request: Request, principal: Reader, order_id: UUID
) -> list[CarrierShippingLabelRead]:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        labels = (
            await session.execute(
                select(CarrierShippingLabel)
                .where(
                    CarrierShippingLabel.tenant_id == principal.tenant_id,
                    CarrierShippingLabel.order_id == order_id,
                )
                .order_by(CarrierShippingLabel.created_at.desc())
            )
        ).scalars()
        return [CarrierShippingLabelRead.model_validate(label) for label in labels]


@router.post(
    "/{order_id}/shipping-label",
    response_model=CarrierShippingLabelRead,
    status_code=status.HTTP_201_CREATED,
)
async def book_order_shipping_label(
    request: Request,
    principal: Shipper,
    order_id: UUID,
    payload: ShippingLabelBookRequest,
) -> CarrierShippingLabelRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        label, _pdf_bytes = await book_shipping_label(
            session,
            _storage(request),
            principal.tenant_id,
            principal,
            order_id,
            payload,
            tracking_adapter=_tracking_adapter(request),
        )
    return label


@router.get("/{order_id}/shipping-labels/{label_id}/pdf", response_class=Response)
async def download_shipping_label_pdf(
    request: Request, principal: Reader, order_id: UUID, label_id: UUID
) -> Response:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        pdf_bytes = await get_label_pdf_bytes(
            session,
            _storage(request),
            principal.tenant_id,
            order_id,
            label_id,
        )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="LABEL-{order_id}-{label_id}.pdf"'
            )
        },
    )
