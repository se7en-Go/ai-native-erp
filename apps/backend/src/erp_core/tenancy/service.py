from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import OnboardingStage, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.cost_visibility import can_view_cost_and_profit
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.schemas import (
    InviteMemberRequest,
    LoginRequest,
    MembershipItem,
    PrincipalResponse,
    RegisterRequest,
    TenantMembershipOption,
    TenantSettingsResponse,
    TokenResponse,
    UpdateMemberRoleRequest,
    UpdateTenantSettingsRequest,
)
from erp_core.tenancy.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or f"tenant-{uuid4().hex[:8]}"


class TenancyAuthService:
    def __init__(self, settings: Settings, crypto_service: EnvelopeCryptoService) -> None:
        self._settings = settings
        self._crypto = crypto_service

    def _build_token_response(self, principal: PrincipalResponse) -> TokenResponse:
        access = create_access_token(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            settings=self._settings,
        )
        refresh = create_refresh_token(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            settings=self._settings,
        )
        return TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            principal=principal,
        )

    async def register_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        req: RegisterRequest,
    ) -> TokenResponse:
        email = str(req.email).lower().strip()
        tenant_id = uuid4()
        user_id = uuid4()
        if req.tenant_slug:
            slug = _slugify(req.tenant_slug)
        else:
            slug = f"{_slugify(req.tenant_name)}-{tenant_id.hex[:6]}"
        password_hash = hash_password(req.password)

        try:
            async with tenant_session(session_factory, tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO tenants "
                        "(id, slug, name, business_preset, base_currency, timezone) "
                        "VALUES (:id, :slug, :name, :preset, :currency, :tz)"
                    ),
                    {
                        "id": str(tenant_id),
                        "slug": slug,
                        "name": req.tenant_name.strip(),
                        "preset": req.preset_type,
                        "currency": req.base_currency.upper(),
                        "tz": req.timezone,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, password_hash) "
                        "VALUES (:id, :email, :full_name, :ph)"
                    ),
                    {
                        "id": str(user_id),
                        "email": email,
                        "full_name": req.full_name.strip(),
                        "ph": password_hash,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                        "VALUES (:id, :tid, :uid, :role)"
                    ),
                    {
                        "id": str(uuid4()),
                        "tid": str(tenant_id),
                        "uid": str(user_id),
                        "role": UserRole.OWNER.value,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO onboarding_states (tenant_id, current_stage, preset_type) "
                        "VALUES (:tid, :stage, :preset)"
                    ),
                    {
                        "tid": str(tenant_id),
                        "stage": OnboardingStage.WORKSPACE_CREATED.value,
                        "preset": req.preset_type,
                    },
                )
                await self._crypto.provision_tenant_dek(session, tenant_id)
        except IntegrityError as exc:
            err_msg = str(exc).lower()
            if "users_email_key" in err_msg or "email" in err_msg:
                raise AppError(
                    slug="email-already-registered",
                    title="Email Already Registered",
                    status_code=409,
                    detail="An account with this email already exists",
                    code="ERR_EMAIL_EXISTS",
                ) from exc
            raise AppError(
                slug="tenant-slug-taken",
                title="Workspace Slug Taken",
                status_code=409,
                detail="Workspace slug is already in use",
                code="ERR_TENANT_SLUG_EXISTS",
            ) from exc

        principal = PrincipalResponse(
            user_id=user_id,
            tenant_id=tenant_id,
            tenant_slug=slug,
            tenant_name=req.tenant_name.strip(),
            role=UserRole.OWNER,
            email=email,
            full_name=req.full_name.strip(),
            can_view_cost=True,
        )
        return self._build_token_response(principal)

    async def login(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        req: LoginRequest,
    ) -> TokenResponse:
        email = str(req.email).lower().strip()
        async with system_session(session_factory) as session:
            user_row = (
                await session.execute(
                    text(
                        "SELECT id, email, full_name, password_hash, is_active "
                        "FROM users WHERE email = :email"
                    ),
                    {"email": email},
                )
            ).first()
            if user_row is None or not user_row.is_active:
                raise AppError(
                    slug="invalid-credentials",
                    title="Invalid Credentials",
                    status_code=401,
                    detail="Invalid email or password",
                    code="ERR_INVALID_CREDENTIALS",
                )
            if not verify_password(req.password, str(user_row.password_hash)):
                raise AppError(
                    slug="invalid-credentials",
                    title="Invalid Credentials",
                    status_code=401,
                    detail="Invalid email or password",
                    code="ERR_INVALID_CREDENTIALS",
                )

            memberships = (
                await session.execute(
                    text("SELECT * FROM resolve_user_memberships(:uid)"),
                    {"uid": str(user_row.id)},
                )
            ).all()

        if not memberships:
            raise AppError(
                slug="no-active-workspace",
                title="No Active Workspace",
                status_code=403,
                detail="No active tenant workspace membership found for user",
                code="ERR_NO_ACTIVE_TENANT",
            )

        selected = memberships[0]
        if req.tenant_slug:
            matched = [m for m in memberships if m.tenant_slug == req.tenant_slug]
            if not matched:
                raise AppError(
                    slug="workspace-access-denied",
                    title="Workspace Access Denied",
                    status_code=403,
                    detail=f"User does not belong to workspace '{req.tenant_slug}'",
                    code="ERR_TENANT_ACCESS_DENIED",
                )
            selected = matched[0]

        role = UserRole(str(selected.role))
        principal = PrincipalResponse(
            user_id=UUID(str(user_row.id)),
            tenant_id=UUID(str(selected.tenant_id)),
            tenant_slug=str(selected.tenant_slug),
            tenant_name=str(selected.tenant_name),
            role=role,
            email=str(user_row.email),
            full_name=str(user_row.full_name),
            can_view_cost=can_view_cost_and_profit(role),
        )
        return self._build_token_response(principal)

    async def refresh_tokens(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        refresh_token: str,
    ) -> TokenResponse:
        payload = decode_token(refresh_token, self._settings, expected_type="refresh")
        async with tenant_session(session_factory, payload.tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT u.id AS user_id, u.email, u.full_name, t.id AS tenant_id, "
                        "t.slug AS tenant_slug, t.name AS tenant_name, m.role "
                        "FROM tenant_memberships m "
                        "JOIN users u ON u.id = m.user_id "
                        "JOIN tenants t ON t.id = m.tenant_id "
                        "WHERE m.tenant_id = :tid AND m.user_id = :uid "
                        "  AND m.is_active = true AND u.is_active = true "
                        "  AND t.is_active = true AND t.shredded_at IS NULL"
                    ),
                    {"tid": str(payload.tid), "uid": str(payload.sub)},
                )
            ).first()
        if row is None:
            raise AppError(
                slug="refresh-denied",
                title="Refresh Token Revoked",
                status_code=401,
                detail="User membership or tenant is inactive",
                code="ERR_INVALID_TOKEN",
            )
        role = UserRole(str(row.role))
        principal = PrincipalResponse(
            user_id=UUID(str(row.user_id)),
            tenant_id=UUID(str(row.tenant_id)),
            tenant_slug=str(row.tenant_slug),
            tenant_name=str(row.tenant_name),
            role=role,
            email=str(row.email),
            full_name=str(row.full_name),
            can_view_cost=can_view_cost_and_profit(role),
        )
        return self._build_token_response(principal)

    async def get_me(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
    ) -> PrincipalResponse:
        row = (
            await session.execute(
                text(
                    "SELECT u.full_name, t.slug AS tenant_slug, t.name AS tenant_name "
                    "FROM tenants t, users u "
                    "WHERE t.id = :tid AND u.id = :uid"
                ),
                {"tid": str(principal.tenant_id), "uid": str(principal.user_id)},
            )
        ).one()
        return PrincipalResponse(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            tenant_slug=str(row.tenant_slug),
            tenant_name=str(row.tenant_name),
            role=principal.role,
            email=principal.email,
            full_name=str(row.full_name),
            can_view_cost=can_view_cost_and_profit(principal.role),
        )

    async def list_my_tenants(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        user_id: UUID,
    ) -> list[TenantMembershipOption]:
        async with system_session(session_factory) as session:
            rows = (
                await session.execute(
                    text("SELECT * FROM resolve_user_memberships(:uid)"),
                    {"uid": str(user_id)},
                )
            ).all()
        return [
            TenantMembershipOption(
                tenant_id=UUID(str(r.tenant_id)),
                tenant_slug=str(r.tenant_slug),
                tenant_name=str(r.tenant_name),
                role=UserRole(str(r.role)),
                is_active=bool(r.is_active),
            )
            for r in rows
        ]

    async def list_tenant_memberships(
        self,
        session: AsyncSession,
        limit: int,
        offset: int,
    ) -> PaginatedResponse[MembershipItem]:
        total = int(await session.scalar(text("SELECT count(*) FROM tenant_memberships")) or 0)
        rows = (
            await session.execute(
                text(
                    "SELECT m.id AS membership_id, m.tenant_id, m.user_id, u.email, "
                    "u.full_name, m.role, m.is_active, m.created_at "
                    "FROM tenant_memberships m "
                    "JOIN users u ON u.id = m.user_id "
                    "ORDER BY m.created_at ASC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
        ).all()
        items = [
            MembershipItem(
                membership_id=UUID(str(r.membership_id)),
                tenant_id=UUID(str(r.tenant_id)),
                user_id=UUID(str(r.user_id)),
                email=str(r.email),
                full_name=str(r.full_name),
                role=UserRole(str(r.role)),
                is_active=bool(r.is_active),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return PaginatedResponse.build(items=items, total=total, limit=limit, offset=offset)

    async def invite_member(
        self,
        session: AsyncSession,
        actor: CurrentPrincipal,
        req: InviteMemberRequest,
    ) -> MembershipItem:
        if req.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise AppError(
                slug="owner-role-restricted",
                title="Owner Assignment Restricted",
                status_code=403,
                detail="Only an existing owner can assign the owner role",
                code="ERR_INSUFFICIENT_ROLE",
            )
        email = str(req.email).lower().strip()
        existing_user = (
            await session.execute(
                text("SELECT id, full_name FROM users WHERE email = :email"),
                {"email": email},
            )
        ).first()
        if existing_user is None:
            user_id = uuid4()
            full_name = req.full_name.strip()
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :fn, :ph)"
                ),
                {
                    "id": str(user_id),
                    "email": email,
                    "fn": full_name,
                    "ph": hash_password(req.password),
                },
            )
        else:
            user_id = UUID(str(existing_user.id))
            full_name = str(existing_user.full_name)

        try:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                        "VALUES (:id, :tid, :uid, :role) "
                        "RETURNING id, tenant_id, user_id, role, is_active, created_at"
                    ),
                    {
                        "id": str(uuid4()),
                        "tid": str(actor.tenant_id),
                        "uid": str(user_id),
                        "role": req.role.value,
                    },
                )
            ).one()
        except IntegrityError as exc:
            raise AppError(
                slug="membership-exists",
                title="Member Already Exists",
                status_code=409,
                detail="User is already a member of this workspace",
                code="ERR_MEMBERSHIP_EXISTS",
            ) from exc

        return MembershipItem(
            membership_id=UUID(str(row.id)),
            tenant_id=UUID(str(row.tenant_id)),
            user_id=UUID(str(row.user_id)),
            email=email,
            full_name=full_name,
            role=UserRole(str(row.role)),
            is_active=bool(row.is_active),
            created_at=row.created_at,
        )

    async def update_member_role(
        self,
        session: AsyncSession,
        actor: CurrentPrincipal,
        membership_id: UUID,
        req: UpdateMemberRoleRequest,
    ) -> MembershipItem:
        if req.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise AppError(
                slug="owner-role-restricted",
                title="Owner Assignment Restricted",
                status_code=403,
                detail="Only an existing owner can assign the owner role",
                code="ERR_INSUFFICIENT_ROLE",
            )
        row = (
            await session.execute(
                text(
                    "UPDATE tenant_memberships "
                    "SET role = :role, is_active = COALESCE(:is_active, is_active) "
                    "WHERE id = :mid AND tenant_id = :tid "
                    "RETURNING id, tenant_id, user_id, role, is_active, created_at"
                ),
                {
                    "role": req.role.value,
                    "is_active": req.is_active,
                    "mid": str(membership_id),
                    "tid": str(actor.tenant_id),
                },
            )
        ).first()
        if row is None:
            raise AppError(
                slug="membership-not-found",
                title="Membership Not Found",
                status_code=404,
                detail="Membership does not exist in this workspace",
                code="ERR_NOT_FOUND",
            )
        user_row = (
            await session.execute(
                text("SELECT email, full_name FROM users WHERE id = :uid"),
                {"uid": str(row.user_id)},
            )
        ).one()
        return MembershipItem(
            membership_id=UUID(str(row.id)),
            tenant_id=UUID(str(row.tenant_id)),
            user_id=UUID(str(row.user_id)),
            email=str(user_row.email),
            full_name=str(user_row.full_name),
            role=UserRole(str(row.role)),
            is_active=bool(row.is_active),
            created_at=row.created_at,
        )

    async def update_tenant_settings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        req: UpdateTenantSettingsRequest,
    ) -> TenantSettingsResponse:
        row = (
            await session.execute(
                text(
                    "UPDATE tenants "
                    "SET name = COALESCE(:name, name), "
                    "    base_currency = COALESCE(:curr, base_currency), "
                    "    timezone = COALESCE(:tz, timezone), "
                    "    updated_at = now() "
                    "WHERE id = :tid "
                    "RETURNING id, slug, name, business_preset, base_currency, timezone"
                ),
                {
                    "name": req.name.strip() if req.name else None,
                    "curr": req.base_currency.upper() if req.base_currency else None,
                    "tz": req.timezone,
                    "tid": str(tenant_id),
                },
            )
        ).one()
        return TenantSettingsResponse(
            tenant_id=UUID(str(row.id)),
            slug=str(row.slug),
            name=str(row.name),
            business_preset=str(row.business_preset),
            base_currency=str(row.base_currency),
            timezone=str(row.timezone),
        )
