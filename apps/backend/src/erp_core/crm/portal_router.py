"""HTTP surface for the B2B buyer self-service portal.

Two routers live here on purpose:

* `portal_operator_router` (`/api/v1/b2b/...`) is JWT + role guarded and only mints,
  lists and revokes magic links;
* `portal_public_router` (`/api/v1/portal/{token}/...`) takes **no** user JWT — the
  opaque `{token}` is the only credential, so every handler resolves it through the
  `SECURITY DEFINER` lookup and then opens a tenant-scoped RLS session.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import UploadFile

from erp_core.crm.portal_service import (
    BuyerPortalViewRead,
    PortalAcceptRequest,
    PortalAcceptResponse,
    PortalLinkCreateRequest,
    PortalLinkCreateResponse,
    PortalLinkRead,
    PortalReceiptRequest,
    PortalReceiptResponse,
    PortalReorderRequest,
    PortalReorderResponse,
    PortalService,
    ResolvedPortalToken,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

portal_operator_router = APIRouter(prefix="/b2b", tags=["b2b-portal"])
portal_public_router = APIRouter(prefix="/portal/{token}", tags=["portal"])

_service = PortalService()

_OPERATOR_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
OperatorPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_OPERATOR_ROLES))]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


def _client_ip(request: Request) -> str | None:
    """Peer address only — never a client-supplied `X-Forwarded-For` header."""
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Operator: magic-link lifecycle
# ---------------------------------------------------------------------------


@portal_operator_router.get(
    "/quotes/{quote_id}/portal-links", response_model=list[PortalLinkRead]
)
async def list_portal_links(
    request: Request,
    principal: OperatorPrincipal,
    quote_id: UUID,
) -> list[PortalLinkRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_portal_links(session, principal.tenant_id, quote_id)


@portal_operator_router.post(
    "/quotes/{quote_id}/portal-links",
    response_model=PortalLinkCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_portal_link(
    request: Request,
    principal: OperatorPrincipal,
    quote_id: UUID,
    payload: PortalLinkCreateRequest,
) -> PortalLinkCreateResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_portal_link(session, principal, quote_id, payload)


@portal_operator_router.delete("/portal-links/{token_id}", response_model=PortalLinkRead)
async def revoke_portal_link(
    request: Request,
    principal: OperatorPrincipal,
    token_id: UUID,
) -> PortalLinkRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.revoke_portal_link(
            session, principal.tenant_id, token_id, str(principal.user_id)
        )


# ---------------------------------------------------------------------------
# Public buyer surface (authenticated solely by the opaque {token})
# ---------------------------------------------------------------------------


async def _resolve(
    request: Request, token: str
) -> tuple[async_sessionmaker[AsyncSession], ResolvedPortalToken]:
    factory = _get_session_factory(request)
    return factory, await _service.resolve_token(factory, token)


@portal_public_router.get("", response_model=BuyerPortalViewRead)
async def get_portal_view(request: Request, token: str) -> BuyerPortalViewRead:
    factory, resolved = await _resolve(request, token)
    async with tenant_session(factory, resolved.tenant_id) as session:
        return await _service.get_portal_view(session, resolved)


@portal_public_router.post("/accept", response_model=PortalAcceptResponse)
async def accept_quote(
    request: Request,
    token: str,
    payload: PortalAcceptRequest,
) -> PortalAcceptResponse:
    factory, resolved = await _resolve(request, token)
    async with tenant_session(factory, resolved.tenant_id) as session:
        return await _service.accept_quote_via_portal(
            session,
            resolved,
            buyer_name=payload.buyer_name,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )


@portal_public_router.post(
    "/reorder",
    response_model=PortalReorderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def reorder_from_portal(
    request: Request,
    token: str,
    payload: PortalReorderRequest | None = None,
) -> PortalReorderResponse:
    """One-click reorder: re-price the same SKUs into a fresh **draft** quotation."""
    body = payload or PortalReorderRequest()
    factory, resolved = await _resolve(request, token)
    async with tenant_session(factory, resolved.tenant_id) as session:
        return await _service.reorder_from_portal(
            session,
            resolved,
            notes=body.notes,
            requested_quantities=body.requested_quantities,
            ip_address=_client_ip(request),
        )


def _receipt_validation_error(exc: ValidationError) -> AppError:
    first = exc.errors()[0] if exc.errors() else {"msg": "Invalid receipt payload."}
    return AppError(
        slug="portal-receipt-invalid",
        status_code=422,
        code="ERR_VALIDATION",
        title="Invalid Receipt Payload",
        detail=str(first.get("msg", "Invalid receipt payload.")),
    )


async def _read_receipt_payload(
    request: Request,
) -> tuple[PortalReceiptRequest, bytes | None, str | None, str | None]:
    """Accept either a multipart form (optional bank-slip file) or a JSON body."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        try:
            payload = PortalReceiptRequest.model_validate(
                {
                    "rp_id": form.get("rp_id"),
                    "payment_method": form.get("payment_method") or "bank_transfer",
                    "external_tx_ref": form.get("external_tx_ref") or None,
                    "amount_received": form.get("amount_received"),
                    "currency": form.get("currency"),
                }
            )
        except ValidationError as exc:
            raise _receipt_validation_error(exc) from None
        upload = form.get("receipt_file") or form.get("receipt")
        if not isinstance(upload, UploadFile):
            return payload, None, None, None
        receipt_bytes = await upload.read()
        return (
            payload,
            receipt_bytes or None,
            upload.filename,
            upload.content_type,
        )

    try:
        raw = await request.json()
    except ValueError:
        raise AppError(
            slug="portal-receipt-invalid",
            status_code=422,
            code="ERR_VALIDATION",
            title="Invalid Receipt Payload",
            detail="Send a JSON object or a multipart form with the receipt fields.",
        ) from None
    if not isinstance(raw, dict):
        raise AppError(
            slug="portal-receipt-invalid",
            status_code=422,
            code="ERR_VALIDATION",
            title="Invalid Receipt Payload",
            detail="The receipt body must be a JSON object.",
        )
    try:
        return PortalReceiptRequest.model_validate(raw), None, None, None
    except ValidationError as exc:
        raise _receipt_validation_error(exc) from None


@portal_public_router.post(
    "/receipt",
    response_model=PortalReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["rp_id", "amount_received", "currency"],
                        "properties": {
                            "rp_id": {"type": "string", "format": "uuid"},
                            "payment_method": {"type": "string"},
                            "external_tx_ref": {"type": "string"},
                            "amount_received": {"type": "string"},
                            "currency": {"type": "string"},
                            "receipt_file": {"type": "string", "format": "binary"},
                        },
                    }
                },
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["rp_id", "amount_received", "currency"],
                        "properties": {
                            "rp_id": {"type": "string", "format": "uuid"},
                            "payment_method": {"type": "string"},
                            "external_tx_ref": {"type": "string"},
                            "amount_received": {"type": "string"},
                            "currency": {"type": "string"},
                        },
                    }
                },
            },
        }
    },
)
async def submit_receipt(
    request: Request,
    token: str,
) -> PortalReceiptResponse:
    payload, receipt_bytes, receipt_filename, receipt_content_type = await _read_receipt_payload(
        request
    )
    factory, resolved = await _resolve(request, token)
    storage = getattr(request.app.state, "storage", None)
    async with tenant_session(factory, resolved.tenant_id) as session:
        return await _service.submit_buyer_receipt(
            session,
            storage,
            resolved,
            rp_id=payload.rp_id,
            payment_method=payload.payment_method,
            external_tx_ref=payload.external_tx_ref,
            amount_received=payload.amount_received,
            currency=payload.currency,
            receipt_bytes=receipt_bytes,
            receipt_filename=receipt_filename,
            receipt_content_type=receipt_content_type,
            ip_address=_client_ip(request),
        )


@portal_public_router.get("/documents/{doc_type}")
async def download_document(
    request: Request,
    token: str,
    doc_type: Literal["pi", "ci", "pl"],
    language: Literal["en", "zh"] = Query(default="en"),
) -> Response:
    factory, resolved = await _resolve(request, token)
    async with tenant_session(factory, resolved.tenant_id) as session:
        pdf_bytes, filename = await _service.render_buyer_document(
            session, resolved, doc_type, language
        )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
