from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.billing.models import UsageLedger
from erp_core.billing.schemas import (
    CreditWalletRead,
    TopupCreditsRequest,
    TopupPaymentRead,
    UsageLedgerRead,
)
from erp_core.billing.stripe_sync import (
    HttpStripeMeterClient,
    StripeMeterSyncService,
    interpret_stripe_event,
    verify_stripe_webhook_signature,
)
from erp_core.billing.wallet_service import FUNDING_KEY_PREFIX, CreditWalletService
from erp_core.shared.config import Settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
Db = Annotated[AsyncSession, Depends(get_tenant_db)]
Reader = Annotated[
    CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.FINANCE))
]
Owner = Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER))]


@router.get("/wallet")
async def get_billing_wallet(session: Db, principal: Reader) -> CreditWalletRead:
    return await CreditWalletService().ensure_wallet(session, tenant_id=principal.tenant_id)


@router.get("/usage")
async def list_billing_usage(
    session: Db,
    principal: Reader,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> PaginatedResponse[UsageLedgerRead]:
    usage = (
        UsageLedger.tenant_id == principal.tenant_id,
        ~UsageLedger.idempotency_key.startswith(FUNDING_KEY_PREFIX),
    )
    total = await session.scalar(select(func.count()).select_from(UsageLedger).where(*usage))
    rows = (
        await session.scalars(
            select(UsageLedger)
            .where(*usage)
            .order_by(
                UsageLedger.created_at.desc(),
                UsageLedger.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return PaginatedResponse.build(
        items=[UsageLedgerRead.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("/topup", status_code=201)
async def topup_credits(body: TopupCreditsRequest, principal: Owner) -> TopupPaymentRead:
    try:
        return await HttpStripeMeterClient().create_topup(
            principal.tenant_id, body.credits, body.reference
        )
    except RuntimeError:
        raise AppError(
            slug="stripe-unavailable",
            title="Stripe Unavailable",
            status_code=503,
            code="ERR_STRIPE_UNAVAILABLE",
            detail="Could not start a credit purchase.",
        ) from None


@router.post("/stripe/webhook")
async def stripe_billing_webhook(
    request: Request, stripe_signature: Annotated[str, Header()] = ""
) -> dict[str, str]:
    raw = await request.body()
    settings: Settings = request.app.state.settings
    if not verify_stripe_webhook_signature(raw, stripe_signature, settings.STRIPE_WEBHOOK_SECRET):
        raise AppError(
            slug="invalid-stripe-signature",
            title="Invalid Stripe Signature",
            status_code=401,
            code="ERR_INVALID_STRIPE_SIGNATURE",
            detail="Webhook signature verification failed.",
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Expected a Stripe event object.")
        event = interpret_stripe_event(payload)
    except (ValueError, UnicodeError):
        raise AppError(
            slug="invalid-stripe-event",
            title="Invalid Stripe Event",
            status_code=422,
            code="ERR_INVALID_STRIPE_EVENT",
            detail="Invalid billing webhook payload.",
        ) from None
    if event is None:
        return {"status": "ignored"}
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with tenant_session(factory, event.tenant_id) as session:
        return await StripeMeterSyncService().handle_billing_event(session, event_payload=payload)
