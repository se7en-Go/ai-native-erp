from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.finance.schemas import (
    FinanceOverviewRead,
    OrderProfitSnapshotRead,
    PaymentCreate,
    PaymentRecordRead,
    PaymentVerifyResponse,
    ProfitSummaryRead,
    ReceivableRead,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import OrderMode, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/finance", tags=["finance"])
_payment_service = PaymentService()
_profit_service = OrderProfitService()
_read_roles = Depends(
    require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
)
_verify_roles = Depends(require_roles(UserRole.OWNER, UserRole.FINANCE))
_required_form = Form(...)
_optional_file = File(default=None)
_gateway_fee_form = Form(default=Decimal("0.00"))


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


@router.get("/overview", response_model=FinanceOverviewRead)
async def get_finance_overview(
    request: Request,
    principal: CurrentPrincipal = _read_roles,
) -> FinanceOverviewRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.get_overview(session, principal.tenant_id)


@router.get("/cvp/export.csv")
async def export_cvp_csv(
    request: Request,
    principal: CurrentPrincipal = _read_roles,
) -> Response:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        csv_body = await _payment_service.export_cvp_csv(session, principal.tenant_id)
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="cvp-snapshots.csv"'},
        )


@router.get("/receivables", response_model=PaginatedResponse[ReceivableRead])
async def list_receivables(
    request: Request,
    order_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = _read_roles,
) -> PaginatedResponse[ReceivableRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.list_receivables(
            session,
            principal.tenant_id,
            order_id=order_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )


@router.post(
    "/receivables/{rp_id}/payments",
    response_model=PaymentRecordRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_receivable_payment(
    request: Request,
    rp_id: UUID,
    payment_method: str = _required_form,
    external_tx_ref: str | None = Form(default=None),
    amount_received: Decimal = _required_form,
    currency: str = _required_form,
    actual_fx_rate: Decimal = _required_form,
    gateway_fee: Decimal = _gateway_fee_form,
    receipt_file: UploadFile | None = _optional_file,
    principal: CurrentPrincipal = _read_roles,
) -> PaymentRecordRead:
    receipt_key: str | None = None
    if receipt_file is not None and receipt_file.filename:
        raw = await receipt_file.read()
        storage = getattr(request.app.state, "storage", None) or ObjectStorageService(
            get_settings()
        )
        receipt_key = await storage.upload_bytes(
            key=f"receipts/{principal.tenant_id}/{rp_id}/{uuid4().hex}",
            data=raw,
            content_type=receipt_file.content_type or "application/octet-stream",
        )
    payload = PaymentCreate(
        rp_id=rp_id,
        payment_method=payment_method,
        external_tx_ref=external_tx_ref,
        amount_received=amount_received,
        currency=currency,
        actual_fx_rate=actual_fx_rate,
        gateway_fee=gateway_fee,
        receipt_r2_key=receipt_key,
    )
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.submit_payment_receipt(
            session, principal.tenant_id, principal, payload
        )


@router.post("/payments", response_model=PaymentRecordRead, status_code=status.HTTP_201_CREATED)
async def record_payment(
    request: Request,
    payload: PaymentCreate,
    principal: CurrentPrincipal = _verify_roles,
) -> PaymentRecordRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.record_and_verify_payment(
            session, principal.tenant_id, principal, payload
        )


@router.post("/payments/{payment_id}/verify", response_model=PaymentVerifyResponse)
async def verify_payment_record(
    request: Request,
    payment_id: UUID,
    principal: CurrentPrincipal = _verify_roles,
) -> PaymentVerifyResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.verify_payment(
            session, principal.tenant_id, principal, payment_id
        )


@router.get("/orders/{order_id}/profit", response_model=OrderProfitSnapshotRead)
async def get_order_profit(
    request: Request,
    order_id: UUID,
    principal: CurrentPrincipal = _read_roles,
) -> OrderProfitSnapshotRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _profit_service.get_order_profit(session, principal.tenant_id, order_id)


@router.get("/profit-summary", response_model=ProfitSummaryRead)
async def get_profit_summary(
    request: Request,
    order_mode: OrderMode | None = None,
    channel_account_id: UUID | None = None,
    principal: CurrentPrincipal = _read_roles,
) -> ProfitSummaryRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _profit_service.get_profit_summary(
            session,
            principal.tenant_id,
            order_mode=order_mode,
            channel_account_id=channel_account_id,
        )
