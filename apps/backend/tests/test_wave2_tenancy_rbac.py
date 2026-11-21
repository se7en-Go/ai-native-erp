"""Wave 2 hardening: AP payment ledger RLS + owner governance (GPT #09 / Muse P2-02).

Real PostgreSQL 17 tests: they exercise the fail-closed RLS policies and the OWNER
governance guards through the production ``TenancyAuthService`` code path.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.schemas import UpdateMemberRoleRequest
from erp_core.tenancy.service import TenancyAuthService


def _service() -> TenancyAuthService:
    settings = get_settings()
    return TenancyAuthService(settings, EnvelopeCryptoService(settings))


async def _create_tenant(session: AsyncSession, tenant_id: UUID, slug: str) -> None:
    await session.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
        {"id": str(tenant_id), "slug": slug, "name": slug.replace("-", " ").title()},
    )


async def _create_member(
    session: AsyncSession,
    tenant_id: UUID,
    role: UserRole,
    *,
    is_active: bool = True,
) -> tuple[UUID, UUID]:
    user_id = uuid4()
    membership_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:id, :email, :fn, 'x')"
        ),
        {"id": str(user_id), "email": f"u-{user_id.hex[:12]}@wave2.example.com", "fn": role.value},
    )
    await session.execute(
        text(
            "INSERT INTO tenant_memberships (id, tenant_id, user_id, role, is_active) "
            "VALUES (:id, :tid, :uid, :role, :active)"
        ),
        {
            "id": str(membership_id),
            "tid": str(tenant_id),
            "uid": str(user_id),
            "role": role.value,
            "active": is_active,
        },
    )
    return user_id, membership_id


# ---------------------------------------------------------------------------
# 0013 payable_payments: FORCE RLS + cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_payable_payments_table_enforces_forced_rls(
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with migrator_session_factory() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'public.payable_payments'::regclass"
                )
            )
        ).one()
        assert flags.relrowsecurity is True
        assert flags.relforcerowsecurity is True

        tenant_scoped_policy = await session.scalar(
            text(
                "SELECT count(*) FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'payable_payments' "
                "  AND permissive = 'RESTRICTIVE' AND roles = '{app_runtime}' "
                "  AND qual LIKE '%app.current_tenant_id%'"
            )
        )
        assert int(tenant_scoped_policy or 0) >= 1

    # No tenant GUC -> fail closed, zero rows even though nothing was filtered by the caller.
    async with system_session(runtime_session_factory) as session:
        assert await session.scalar(text("SELECT count(*) FROM payable_payments")) == 0


async def test_payable_payments_isolates_tenant_a_from_tenant_b(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    payable_a = uuid4()

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await _create_tenant(session, tenant_a, f"ap-a-{tenant_a.hex[:8]}")
        await session.execute(
            text(
                "INSERT INTO payables (id, tenant_id, po_no, supplier_name, due_amount, currency) "
                "VALUES (:id, :tid, :po, :sup, 1000.00, 'CNY')"
            ),
            {"id": str(payable_a), "tid": str(tenant_a), "po": "PO-A-1", "sup": "Supplier A"},
        )
        await session.execute(
            text(
                "INSERT INTO payable_payments "
                "(tenant_id, payable_id, amount, currency, payment_ref, idempotency_key) "
                "VALUES (:tid, :pid, 400.00, 'CNY', 'TX-A-1', 'idem-a-1')"
            ),
            {"tid": str(tenant_a), "pid": str(payable_a)},
        )

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await _create_tenant(session, tenant_b, f"ap-b-{tenant_b.hex[:8]}")

    # Tenant B sees nothing of Tenant A, with or without an explicit filter.
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        assert await session.scalar(text("SELECT count(*) FROM payable_payments")) == 0
        leaked = (
            await session.execute(
                text("SELECT id, tenant_id FROM payable_payments WHERE payable_id = :pid"),
                {"pid": str(payable_a)},
            )
        ).all()
        assert leaked == []

        # Cross-tenant write MUST be rejected by the RESTRICTIVE WITH CHECK policy.
        with pytest.raises(DBAPIError) as exc_info:
            await session.execute(
                text(
                    "INSERT INTO payable_payments "
                    "(tenant_id, payable_id, amount, idempotency_key) "
                    "VALUES (:tid, :pid, 10.00, 'idem-leak')"
                ),
                {"tid": str(tenant_a), "pid": str(payable_a)},
            )
        assert "row-level security" in str(exc_info.value).lower()

    # Tenant A still sees its own ledger row.
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        rows = (
            await session.execute(
                text("SELECT tenant_id, payable_id, amount FROM payable_payments")
            )
        ).all()
        assert len(rows) == 1
        assert UUID(str(rows[0].tenant_id)) == tenant_a
        assert UUID(str(rows[0].payable_id)) == payable_a


# ---------------------------------------------------------------------------
# OWNER governance: an ADMIN can never touch an OWNER membership
# ---------------------------------------------------------------------------


async def test_admin_cannot_demote_or_deactivate_owner(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await _create_tenant(session, tenant_id, f"gov-a-{tenant_id.hex[:8]}")
        owner_user, owner_membership = await _create_member(session, tenant_id, UserRole.OWNER)
        admin_user, _admin_membership = await _create_member(session, tenant_id, UserRole.ADMIN)

        admin_principal = CurrentPrincipal(
            user_id=admin_user,
            tenant_id=tenant_id,
            role=UserRole.ADMIN,
            email=f"admin-{admin_user.hex[:8]}@wave2.example.com",
        )
        service = _service()

        with pytest.raises(AppError) as demote_err:
            await service.update_member_role(
                session,
                admin_principal,
                owner_membership,
                UpdateMemberRoleRequest(role=UserRole.MANAGER),
            )
        assert demote_err.value.status_code == 403
        assert demote_err.value.code == "ERR_INSUFFICIENT_ROLE"

        with pytest.raises(AppError) as deactivate_err:
            await service.update_member_role(
                session,
                admin_principal,
                owner_membership,
                UpdateMemberRoleRequest(role=UserRole.OWNER, is_active=False),
            )
        assert deactivate_err.value.status_code == 403
        assert deactivate_err.value.code == "ERR_INSUFFICIENT_ROLE"

        # The owner row is untouched after both rejected attempts.
        owner_row = (
            await session.execute(
                text("SELECT role, is_active FROM tenant_memberships WHERE id = :mid"),
                {"mid": str(owner_membership)},
            )
        ).one()
        assert owner_row.role == UserRole.OWNER.value
        assert owner_row.is_active is True
        assert owner_user is not None

    # An ADMIN may still manage non-owner memberships.
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        admin_user = (
            await session.execute(
                text(
                    "SELECT user_id FROM tenant_memberships "
                    "WHERE tenant_id = :tid AND role = 'admin'"
                ),
                {"tid": str(tenant_id)},
            )
        ).scalar_one()
        manager_user, manager_membership = await _create_member(
            session, tenant_id, UserRole.MANAGER
        )
        updated = await _service().update_member_role(
            session,
            CurrentPrincipal(
                user_id=UUID(str(admin_user)),
                tenant_id=tenant_id,
                role=UserRole.ADMIN,
                email=f"admin-{tenant_id.hex[:8]}@wave2.example.com",
            ),
            manager_membership,
            UpdateMemberRoleRequest(role=UserRole.AGENT),
        )
        assert updated.role == UserRole.AGENT
        assert updated.user_id == manager_user


# ---------------------------------------------------------------------------
# OWNER governance: the last active owner of a tenant is protected
# ---------------------------------------------------------------------------


async def test_owner_cannot_demote_or_deactivate_last_active_owner(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await _create_tenant(session, tenant_id, f"gov-last-{tenant_id.hex[:8]}")
        owner_user, owner_membership = await _create_member(session, tenant_id, UserRole.OWNER)
        # An inactive owner MUST NOT be counted as a still-standing active owner.
        await _create_member(session, tenant_id, UserRole.OWNER, is_active=False)

        owner_principal = CurrentPrincipal(
            user_id=owner_user,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            email=f"owner-{owner_user.hex[:8]}@wave2.example.com",
        )
        service = _service()

        with pytest.raises(AppError) as demote_err:
            await service.update_member_role(
                session,
                owner_principal,
                owner_membership,
                UpdateMemberRoleRequest(role=UserRole.ADMIN),
            )
        assert demote_err.value.status_code == 409
        assert demote_err.value.code == "ERR_LAST_OWNER_PROTECTED"

        with pytest.raises(AppError) as deactivate_err:
            await service.update_member_role(
                session,
                owner_principal,
                owner_membership,
                UpdateMemberRoleRequest(role=UserRole.OWNER, is_active=False),
            )
        assert deactivate_err.value.status_code == 409
        assert deactivate_err.value.code == "ERR_LAST_OWNER_PROTECTED"

        row = (
            await session.execute(
                text("SELECT role, is_active FROM tenant_memberships WHERE id = :mid"),
                {"mid": str(owner_membership)},
            )
        ).one()
        assert row.role == UserRole.OWNER.value
        assert row.is_active is True


async def test_owner_can_demote_one_of_two_active_owners(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await _create_tenant(session, tenant_id, f"gov-two-{tenant_id.hex[:8]}")
        first_owner, _first_membership = await _create_member(session, tenant_id, UserRole.OWNER)
        _second_owner, second_membership = await _create_member(
            session, tenant_id, UserRole.OWNER
        )

        updated = await _service().update_member_role(
            session,
            CurrentPrincipal(
                user_id=first_owner,
                tenant_id=tenant_id,
                role=UserRole.OWNER,
                email=f"owner-{first_owner.hex[:8]}@wave2.example.com",
            ),
            second_membership,
            UpdateMemberRoleRequest(role=UserRole.ADMIN),
        )
        assert updated.role == UserRole.ADMIN
        assert updated.is_active is True

        remaining_active_owners = await session.scalar(
            text(
                "SELECT count(*) FROM tenant_memberships "
                "WHERE tenant_id = :tid AND role = 'owner' AND is_active = true"
            ),
            {"tid": str(tenant_id)},
        )
        assert remaining_active_owners == 1
