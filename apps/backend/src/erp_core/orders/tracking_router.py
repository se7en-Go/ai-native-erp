from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.webhook_delivery import accept_delivery
from erp_core.orders.models import Shipment
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import ShipmentRead, ShipOrderRequest
from erp_core.orders.tracking_adapter import Tracking17TrackAdapter, verify_17track_signature
from erp_core.orders.tracking_worker import apply_tracking_update
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import TrackingProvider, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(tags=["logistics"])
_order_service = OrderService()
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
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
Refresher = Annotated[
    CurrentPrincipal,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.MANAGER,
            UserRole.AGENT,
        )
    ),
]


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _tracking_adapter(request: Request) -> Tracking17TrackAdapter:
    adapter: Tracking17TrackAdapter | None = getattr(request.app.state, "tracking_adapter", None)
    return adapter or Tracking17TrackAdapter()


@router.post("/webhooks/17track")
async def receive_17track_webhook(
    request: Request,
    sign: Annotated[str | None, Header()] = None,
) -> dict[str, int | str]:
    raw = await request.body()
    if not verify_17track_signature(raw, sign or "", os.getenv("TRACK17_WEBHOOK_SECRET", "")):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid 17TRACK Signature",
            detail="Webhook signature verification failed.",
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Payload is not an object")
    except ValueError as exc:
        raise AppError(
            slug="invalid-tracking-delivery",
            status_code=400,
            code="ERR_INVALID_TRACKING_DELIVERY",
            title="Invalid 17TRACK Delivery",
            detail="Expected a JSON object.",
        ) from exc
    await accept_delivery(
        request,
        channel="17track",
        external_event_id=hashlib.sha256(raw).hexdigest(),
        payload=payload,
    )
    return {"code": 200, "status": "accepted"}


@router.get("/orders/{order_id}/shipments", response_model=list[ShipmentRead])
async def list_order_shipments(
    request: Request,
    order_id: UUID,
    principal: Reader,
) -> list[ShipmentRead]:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        result = await session.scalars(
            select(Shipment)
            .where(
                Shipment.tenant_id == principal.tenant_id,
                Shipment.order_id == order_id,
            )
            .order_by(Shipment.created_at.desc())
        )
        return [ShipmentRead.model_validate(shipment) for shipment in result]


@router.post("/orders/{order_id}/shipments", status_code=status.HTTP_201_CREATED)
async def create_order_shipment(
    request: Request,
    order_id: UUID,
    payload: ShipOrderRequest,
    principal: Shipper,
) -> ShipmentRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await _order_service.ship_order(
            session,
            principal.tenant_id,
            principal=principal,
            order_id=order_id,
            payload=payload,
            tracking_adapter=_tracking_adapter(request),
        )


class RefreshTrackingRequest(BaseModel):
    cache_level: Literal[0] = 0


@router.post("/orders/{order_id}/shipments/{shipment_id}/refresh")
@router.post("/orders/{order_id}/shipments/{shipment_id}/refresh-tracking")
async def refresh_shipment_status(
    request: Request,
    order_id: UUID,
    shipment_id: UUID,
    principal: Refresher,
    payload: RefreshTrackingRequest | None = None,
) -> ShipmentRead:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        shipment = await session.scalar(
            select(Shipment)
            .where(
                Shipment.tenant_id == principal.tenant_id,
                Shipment.order_id == order_id,
                Shipment.id == shipment_id,
            )
            .with_for_update()
        )
        if shipment is None or shipment.tracking_provider != TrackingProvider.TRACK17:
            raise AppError(
                slug="shipment-not-found",
                status_code=404,
                code="ERR_SHIPMENT_NOT_FOUND",
                title="Shipment Not Found",
                detail="No 17TRACK shipment was found.",
            )
        now = datetime.now(UTC)
        if shipment.last_synced_at and now - shipment.last_synced_at < timedelta(hours=6):
            raise AppError(
                slug="tracking-cooldown",
                status_code=429,
                code="ERR_TRACKING_COOLDOWN",
                title="Tracking Refresh Cooldown",
                detail="Refresh is limited to once per 6 hours.",
            )
        try:
            result = await _tracking_adapter(request).fetch_status(
                shipment.tracking_number,
                cache_level=payload.cache_level if payload else 0,
            )
            accepted = result["data"]["accepted"]
            if result["code"] != 0 or not accepted:
                raise ValueError("17TRACK did not accept the request")
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise AppError(
                slug="tracking-upstream",
                status_code=502,
                code="ERR_TRACKING_UPSTREAM",
                title="Tracking Provider Error",
                detail="17TRACK did not return tracking data.",
            ) from exc
        shipment.last_synced_at = now
        await session.flush()
        await apply_tracking_update(session, principal.tenant_id, shipment.id, accepted[0])
        await session.refresh(shipment)
        return ShipmentRead.model_validate(shipment)
