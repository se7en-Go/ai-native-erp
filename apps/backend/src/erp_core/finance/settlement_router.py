"""`/api/v1/finance` settlement reconciliation routes.

The router owns the fully-qualified ``/api/v1/finance`` prefix (already admitted
by the operator BFF allowlist) so the composition root only needs
``include_router(settlement_router)``.

RBAC is enforced twice: the FastAPI dependency rejects a foreign role with
``HTTP 403`` before the handler runs, and `SettlementService._assert_writer`
re-checks the mutation roles. `AGENT` is denied on every route in this module -
reads included - because settlement, payout, dual-basis profit and journal data
are the tenant's full financial picture.

`POST /settlements/import` and `POST /payouts/import` accept either a JSON body
(structured rows, optionally carrying `csv_text`) or a raw `text/csv` body whose
batch header comes from the query string, so a bookkeeper can paste a statement
without shaping JSON.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.settlement_service import (
    JOURNAL_FORMATS,
    SETTLEMENT_READ_ROLES,
    SETTLEMENT_WRITE_ROLES,
    DualBasisProfitRead,
    PayoutAutoMatchResultRead,
    PayoutImportRequest,
    PayoutImportResultRead,
    PayoutStatementListRead,
    SettlementBatchListRead,
    SettlementBatchRead,
    SettlementImportRequest,
    SettlementLineListRead,
    SettlementService,
)
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/api/v1/finance", tags=["finance-settlement"])

_service = SettlementService()

TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
SettlementReader = Annotated[CurrentPrincipal, Depends(require_roles(*SETTLEMENT_READ_ROLES))]
SettlementWriter = Annotated[CurrentPrincipal, Depends(require_roles(*SETTLEMENT_WRITE_ROLES))]

#: Content types treated as a raw CSV upload rather than a JSON document.
_CSV_CONTENT_TYPES: tuple[str, ...] = ("text/csv", "text/plain", "application/csv")


def _is_csv_body(request: Request) -> bool:
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return content_type in _CSV_CONTENT_TYPES


async def _json_body(request: Request) -> object:
    raw = await request.body()
    if not raw.strip():
        raise AppError(
            slug="empty-request-body",
            status_code=422,
            code="ERR_EMPTY_REQUEST_BODY",
            title="Empty Request Body",
            detail="A JSON body or a CSV payload is required.",
        )
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            slug="invalid-json-body",
            status_code=422,
            code="ERR_INVALID_JSON",
            title="Invalid JSON Body",
            detail="The request body is not valid JSON.",
        ) from exc
    return parsed


def _inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Resolve a pydantic `$defs` graph into one self-contained JSON Schema object.

    FastAPI only documents a request body when it is a declared body parameter, but
    these two routes must also accept a raw `text/csv` payload. Declaring the JSON
    shape through `openapi_extra` keeps the generated TypeScript client fully typed
    without giving up the raw-CSV branch.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref[len("#/$defs/") :], {})
                return resolve(target)
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved = resolve(schema)
    return resolved if isinstance(resolved, dict) else {}


def _import_operations(model: type[BaseModel]) -> dict[str, Any]:
    """`openapi_extra` documenting a JSON body plus the accepted raw CSV media type."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": _inline_schema(model)},
                "text/csv": {"schema": {"type": "string"}},
            },
        }
    }


def _validation_error(exc: ValidationError) -> AppError:
    return AppError(
        slug="invalid-settlement-payload",
        status_code=422,
        code="ERR_INVALID_SETTLEMENT_PAYLOAD",
        title="Invalid Settlement Payload",
        detail="; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        ),
    )


def _require_query(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise AppError(
            slug="missing-import-header",
            status_code=422,
            code="ERR_MISSING_IMPORT_HEADER",
            title="Missing Import Header",
            detail=f"A raw CSV upload requires the '{name}' query parameter.",
        )
    return value.strip()


def _require_query_date(value: date | None, name: str) -> date:
    if value is None:
        raise AppError(
            slug="missing-import-header",
            status_code=422,
            code="ERR_MISSING_IMPORT_HEADER",
            title="Missing Import Header",
            detail=f"A raw CSV upload requires the '{name}' query parameter (YYYY-MM-DD).",
        )
    return value


def _require_query_rate(value: Decimal | None) -> Decimal:
    if value is None:
        raise AppError(
            slug="missing-import-header",
            status_code=422,
            code="ERR_MISSING_IMPORT_HEADER",
            title="Missing Import Header",
            detail="A raw CSV upload requires the 'fx_rate_to_base' query parameter.",
        )
    return value


# --------------------------------------------------------------------------- #
# Settlements
# --------------------------------------------------------------------------- #
@router.get("/settlements", response_model=SettlementBatchListRead)
async def list_settlements(
    session: TenantDB,
    principal: SettlementReader,
    channel_type: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SettlementBatchListRead:
    """Imported marketplace payout statements with their base-currency net payout."""
    return await _service.list_settlements(
        session,
        principal.tenant_id,
        channel_type=channel_type,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/settlements/import",
    response_model=SettlementBatchRead,
    openapi_extra=_import_operations(SettlementImportRequest),
)
async def import_settlements(
    request: Request,
    session: TenantDB,
    principal: SettlementWriter,
    channel_type: Annotated[str | None, Query()] = None,
    batch_ref: Annotated[str | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    period_start: Annotated[date | None, Query()] = None,
    period_end: Annotated[date | None, Query()] = None,
    fx_rate_to_base: Annotated[Decimal | None, Query(gt=0)] = None,
    channel_account_id: Annotated[UUID | None, Query()] = None,
) -> SettlementBatchRead:
    """Import one statement from a JSON body or a raw CSV body.

    Raw CSV requires the batch header (`channel_type`, `batch_ref`, `currency`,
    `period_start`, `period_end`, `fx_rate_to_base`) as query parameters; the
    body then carries only the per-order rows.
    """
    if _is_csv_body(request):
        payload = SettlementImportRequest(
            channel_type=_require_query(channel_type, "channel_type"),
            batch_ref=_require_query(batch_ref, "batch_ref"),
            currency=_require_query(currency, "currency"),
            period_start=_require_query_date(period_start, "period_start"),
            period_end=_require_query_date(period_end, "period_end"),
            fx_rate_to_base=_require_query_rate(fx_rate_to_base),
            channel_account_id=channel_account_id,
            csv_text=(await request.body()).decode("utf-8", errors="strict"),
        )
    else:
        body = await _json_body(request)
        try:
            payload = SettlementImportRequest.model_validate(body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
    return await _service.import_settlements(session, principal.tenant_id, principal, payload)


@router.get("/settlements/{batch_id}/lines", response_model=SettlementLineListRead)
async def list_settlement_lines(
    batch_id: UUID,
    session: TenantDB,
    principal: SettlementReader,
) -> SettlementLineListRead:
    """Per-order settlement rows of one batch, with their order auto-link state."""
    return await _service.list_settlement_lines(session, principal.tenant_id, batch_id)


# --------------------------------------------------------------------------- #
# Payout statements
# --------------------------------------------------------------------------- #
@router.get("/payouts", response_model=PayoutStatementListRead)
async def list_payouts(
    session: TenantDB,
    principal: SettlementReader,
    provider: Annotated[str | None, Query()] = None,
    match_status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PayoutStatementListRead:
    """Cross-border collection statements with their realised FX gain/loss."""
    return await _service.list_payouts(
        session,
        principal.tenant_id,
        provider=provider,
        match_status=match_status,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/payouts/import",
    response_model=PayoutImportResultRead,
    openapi_extra=_import_operations(PayoutImportRequest),
)
async def import_payouts(
    request: Request,
    session: TenantDB,
    principal: SettlementWriter,
    provider: Annotated[str | None, Query()] = None,
) -> PayoutImportResultRead:
    """Import PingPong / LianLian / Payoneer / bank T/T / Stripe statements."""
    body: object
    if _is_csv_body(request):
        raw_csv = (await request.body()).decode("utf-8", errors="strict")
        body = {
            "provider": _require_query(provider, "provider").lower(),
            "csv_text": raw_csv,
        }
    else:
        body = await _json_body(request)
    try:
        payload = PayoutImportRequest.model_validate(body)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return await _service.import_payouts(session, principal.tenant_id, principal, payload)


@router.post("/payouts/auto-match", response_model=PayoutAutoMatchResultRead)
async def auto_match_payouts(
    session: TenantDB,
    principal: SettlementWriter,
) -> PayoutAutoMatchResultRead:
    """Match unmatched payouts to open settlement batches or paid B2B orders."""
    return await _service.auto_match_payouts(session, principal.tenant_id, principal)


# --------------------------------------------------------------------------- #
# Dual-basis profit + voucher export
# --------------------------------------------------------------------------- #
@router.get("/profit/dual-basis", response_model=DualBasisProfitRead)
async def get_dual_basis_profit(
    session: TenantDB,
    principal: SettlementReader,
) -> DualBasisProfitRead:
    """Estimated CVP basis versus settled marketplace basis, order by order."""
    return await _service.get_dual_basis_profit(session, principal.tenant_id)


@router.get("/journal-export")
async def export_journal_csv(
    session: TenantDB,
    principal: SettlementReader,
    export_format: Annotated[str, Query(alias="format")] = "standard",
) -> Response:
    """Balanced Debit/Credit voucher CSV in `standard`, `quickbooks`, `xero` or `kingdee`."""
    if export_format not in JOURNAL_FORMATS:
        raise AppError(
            slug="unsupported-journal-format",
            status_code=422,
            code="ERR_UNSUPPORTED_JOURNAL_FORMAT",
            title="Unsupported Journal Format",
            detail=f"format must be one of {', '.join(JOURNAL_FORMATS)}.",
        )
    body = await _service.export_journal_csv(session, principal.tenant_id, export_format)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": (f'attachment; filename="journal-{export_format}.csv"')},
    )
