from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import list_audit_events
from erp_core.tenancy.cost_visibility import apply_cost_visibility
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)
from erp_core.tenancy.gdpr_service import GdprService
from erp_core.tenancy.schemas import (
    AuditEventRead,
    CostVisibilitySampleResponse,
    GdprAnonymizeResult,
    GdprExportResult,
    GdprTenantRedactResult,
    InviteMemberRequest,
    LoginRequest,
    MembershipItem,
    PrincipalResponse,
    RefreshRequest,
    RegisterRequest,
    TenantMembershipOption,
    TenantSettingsResponse,
    TokenResponse,
    UpdateMemberRoleRequest,
    UpdateTenantSettingsRequest,
)
from erp_core.tenancy.service import TenancyAuthService

router = APIRouter(prefix="/api/v1", tags=["tenancy"])


def _get_auth_service(request: Request) -> TenancyAuthService:
    settings: Settings = request.app.state.settings
    crypto_service: EnvelopeCryptoService = request.app.state.crypto_service
    return TenancyAuthService(settings=settings, crypto_service=crypto_service)


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.register_workspace(session_factory, payload)


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.login(session_factory, payload)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.refresh_tokens(session_factory, payload.refresh_token)


@router.get("/auth/me", response_model=PrincipalResponse)
async def get_me(
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> PrincipalResponse:
    return await service.get_me(session, principal)


@router.get("/auth/my-tenants", response_model=list[TenantMembershipOption])
async def list_my_tenants(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> list[TenantMembershipOption]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.list_my_tenants(session_factory, principal.user_id)


@router.get("/tenants/memberships", response_model=PaginatedResponse[MembershipItem])
async def list_memberships(
    _principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[MembershipItem]:
    return await service.list_tenant_memberships(session, limit=limit, offset=offset)


@router.post(
    "/tenants/memberships",
    response_model=MembershipItem,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    payload: InviteMemberRequest,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> MembershipItem:
    return await service.invite_member(session, principal, payload)


@router.patch("/tenants/memberships/{membership_id}", response_model=MembershipItem)
async def update_member_role(
    membership_id: UUID,
    payload: UpdateMemberRoleRequest,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> MembershipItem:
    return await service.update_member_role(session, principal, membership_id, payload)


@router.patch("/tenants/settings", response_model=TenantSettingsResponse)
async def update_tenant_settings(
    payload: UpdateTenantSettingsRequest,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TenantSettingsResponse:
    return await service.update_tenant_settings(session, principal.tenant_id, payload)


@router.get("/tenants/cost-visibility-sample", response_model=CostVisibilitySampleResponse)
async def get_cost_visibility_sample(
    principal: Annotated[
        CurrentPrincipal,
        Depends(
            require_roles(
                UserRole.OWNER,
                UserRole.ADMIN,
                UserRole.MANAGER,
                UserRole.AGENT,
                UserRole.FINANCE,
            )
        ),
    ],
) -> CostVisibilitySampleResponse:
    sample = CostVisibilitySampleResponse(
        sku="LED-HB-150W-IP65",
        currency="USD",
        unit_sale_price=Decimal("34.90"),
        cost_price_base=Decimal("18.50"),
        cogs_base=Decimal("18.50"),
        net_profit_base=Decimal("11.20"),
        net_margin_pct=Decimal("32.09"),
        cost_masked=False,
    )
    return apply_cost_visibility(sample, principal.role)


@router.get("/tenancy/audit-events", response_model=PaginatedResponse[AuditEventRead])
async def get_audit_events(
    _principal: Annotated[
        CurrentPrincipal,
        Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.FINANCE)),
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    action: str | None = None,
    resource_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[AuditEventRead]:
    return await list_audit_events(
        session,
        action=action,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )


@router.post("/tenancy/gdpr/customers/{customer_id}/export", response_model=GdprExportResult)
async def gdpr_export_customer(
    customer_id: UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprExportResult:
    storage: ObjectStorageService = request.app.state.storage
    gdpr = GdprService()
    return await gdpr.export_customer_data(
        session,
        storage,
        principal.tenant_id,
        customer_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/tenancy/gdpr/customers/{customer_id}/anonymize",
    response_model=GdprAnonymizeResult,
)
async def gdpr_anonymize_customer(
    customer_id: UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprAnonymizeResult:
    gdpr = GdprService()
    return await gdpr.anonymize_customer_pii(
        session,
        principal.tenant_id,
        customer_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/tenancy/gdpr/tenant/redact", response_model=GdprTenantRedactResult)
async def gdpr_redact_tenant(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprTenantRedactResult:
    storage: ObjectStorageService = request.app.state.storage
    crypto_service: EnvelopeCryptoService = request.app.state.crypto_service
    gdpr = GdprService()
    return await gdpr.redact_tenant_crypto_shred(
        session,
        storage,
        crypto_service,
        principal.tenant_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
