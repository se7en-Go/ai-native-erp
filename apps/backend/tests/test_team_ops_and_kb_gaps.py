"""Track 6 backend suite: team store scopes, offboarding handover, red-line guard,
knowledge-base gap promotion, and AI adoption metrics.

Everything runs against the real PG17 test database as the non-superuser
`app_runtime` role through `tenant_session`, so every assertion also proves the
`FORCE ROW LEVEL SECURITY` policies on the three new tables (0025) rather than a
mocked repository layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.kb_gap_router import router as kb_gap_router
from erp_core.ai_engine.kb_gap_schemas import KbGapCreateRequest, RedlineRuleCreate
from erp_core.ai_engine.kb_gap_service import KbGapService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password
from erp_core.tenancy.store_scope_router import router as store_scope_router
from erp_core.tenancy.store_scope_schemas import StoreScopeAssignRequest
from erp_core.tenancy.store_scope_service import StoreScopeService

RuntimeFactory = async_sessionmaker[AsyncSession]

SCOPE_ACTION = "tenancy.store_scope"
HANDOVER_ACTION = "tenancy.member.handover"


# ----------------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    current: dict[str, CurrentPrincipal] = field(default_factory=dict)

    def act_as(self, principal: CurrentPrincipal) -> None:
        self.current["principal"] = principal

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )


def build_harness(factory: RuntimeFactory) -> Harness:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.include_router(store_scope_router)
    app.include_router(kb_gap_router)
    harness = Harness(app=app)

    async def _principal() -> CurrentPrincipal:
        return harness.current["principal"]

    app.dependency_overrides[get_current_principal] = _principal
    return harness


def principal(tenant_id: UUID, user_id: UUID, role: UserRole) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=f"{role.value}-{user_id.hex[:8]}@factory.test",
    )


# ----------------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------------


async def create_workspace(factory: RuntimeFactory, label: str = "TeamOps") -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, :name, :slug)"),
            {
                "tid": tenant_id,
                "name": f"{label} Co",
                "slug": f"{label.lower()}-{tenant_id.hex[:12]}",
            },
        )
    return tenant_id


async def add_member(factory: RuntimeFactory, tenant_id: UUID, role: UserRole) -> UUID:
    user_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, :name, :ph)"
            ),
            {
                "uid": user_id,
                "email": f"{role.value}-{user_id.hex[:12]}@teamops.test",
                "name": f"{role.value.title()} Person",
                "ph": hash_password("TeamOpsPass2026!"),
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, :role)"
            ),
            {"mid": uuid4(), "tid": tenant_id, "uid": user_id, "role": role.value},
        )
    return user_id


async def seed_account(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    channel: str = "email",
    display_name: str = "Sales inbox",
    owner_user_id: UUID | None = None,
) -> UUID:
    account_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, owner_user_id) "
                "VALUES (:id, :tid, :channel, :external, :name, 'active', :owner)"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "channel": channel,
                "external": f"acct-{account_id.hex[:12]}",
                "name": display_name,
                "owner": owner_user_id,
            },
        )
    return account_id


async def seed_customer(factory: RuntimeFactory, tenant_id: UUID) -> UUID:
    customer_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Buyer GmbH')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
    return customer_id


async def seed_conversation(
    factory: RuntimeFactory,
    tenant_id: UUID,
    account_id: UUID,
    *,
    assigned_user_id: UUID | None = None,
) -> UUID:
    customer_id = await seed_customer(factory, tenant_id)
    conversation_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, external_thread_id, assigned_user_id, unread_count) "
                "VALUES (:id, :tid, :cid, :aid, 'email', :thread, :assignee, 2)"
            ),
            {
                "id": conversation_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": account_id,
                "thread": f"thread-{conversation_id.hex[:10]}",
                "assignee": assigned_user_id,
            },
        )
    return conversation_id


async def seed_opportunity(factory: RuntimeFactory, tenant_id: UUID, owner_user_id: UUID) -> UUID:
    customer_id = await seed_customer(factory, tenant_id)
    opportunity_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, customer_id, title, stage, "
                "expected_amount, currency, owner_user_id) "
                "VALUES (:id, :tid, :cid, 'LED retrofit', 'quoting', 12500.00, 'USD', :owner)"
            ),
            {
                "id": opportunity_id,
                "tid": tenant_id,
                "cid": customer_id,
                "owner": owner_user_id,
            },
        )
    return opportunity_id


async def seed_proposal(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    proposal_type: str,
    status_value: str,
) -> UUID:
    proposal_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                "proposed_payload, confidence_score, status) "
                "VALUES (:id, :tid, 'cs_support', :ptype, '{}'::jsonb, 0.900, :status)"
            ),
            {
                "id": proposal_id,
                "tid": tenant_id,
                "ptype": proposal_type,
                "status": status_value,
            },
        )
    return proposal_id


async def audit_actions(factory: RuntimeFactory, tenant_id: UUID) -> list[str]:
    async with tenant_session(factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action FROM audit_events WHERE tenant_id = :tid "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"tid": tenant_id},
            )
        ).scalars()
        return [str(row) for row in rows]


async def owned_counts(factory: RuntimeFactory, tenant_id: UUID, user_id: UUID) -> dict[str, int]:
    async with tenant_session(factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM opportunities
                          WHERE tenant_id = :tid AND owner_user_id = :uid) AS opportunities,
                        (SELECT COUNT(*) FROM conversations
                          WHERE tenant_id = :tid AND assigned_user_id = :uid) AS conversations,
                        (SELECT COUNT(*) FROM channel_accounts
                          WHERE tenant_id = :tid AND owner_user_id = :uid) AS channel_accounts,
                        (SELECT COUNT(*) FROM member_channel_scopes
                          WHERE tenant_id = :tid AND user_id = :uid) AS store_scopes
                    """
                ),
                {"tid": tenant_id, "uid": user_id},
            )
        ).one()
        return {
            "opportunities": int(row.opportunities),
            "conversations": int(row.conversations),
            "channel_accounts": int(row.channel_accounts),
            "store_scopes": int(row.store_scopes),
        }


# ----------------------------------------------------------------------------
# 1. Migration: FORCE RLS, policies, grants, and CHECK constraints
# ----------------------------------------------------------------------------


async def test_migration_0025_forces_rls_and_grants_app_runtime(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Migration")
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        for table in ("member_channel_scopes", "outbound_redline_rules", "kb_gap_items"):
            flags = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).one()
            assert flags.relrowsecurity is True, table
            assert flags.relforcerowsecurity is True, table

            policies = {
                str(name)
                for name in (
                    await session.execute(
                        text("SELECT polname FROM pg_policy WHERE polrelid = to_regclass(:t)"),
                        {"t": f"public.{table}"},
                    )
                ).scalars()
            }
            assert {"tenant_allow_runtime", "tenant_isolation_policy"} <= policies, table

            grants = {
                str(priv)
                for priv in (
                    await session.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE table_name = :t AND grantee = 'app_runtime'"
                        ),
                        {"t": table},
                    )
                ).scalars()
            }
            assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= grants, table

    # The access-level CHECK is enforced by the database, not only by the API.
    member_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    account_id = await seed_account(runtime_session_factory, tenant_id)
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO member_channel_scopes "
                    "(tenant_id, user_id, channel_account_id, access_level) "
                    "VALUES (:tid, :uid, :caid, 'god_mode')"
                ),
                {"tid": tenant_id, "uid": member_id, "caid": account_id},
            )

    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO outbound_redline_rules (tenant_id, name, pattern, action) "
                    "VALUES (:tid, 'Bad action', 'x', 'explode')"
                ),
                {"tid": tenant_id},
            )

    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO kb_gap_items (tenant_id, customer_question, frequency_count) "
                    "VALUES (:tid, 'Zero frequency', 0)"
                ),
                {"tid": tenant_id},
            )


async def test_new_tables_isolate_tenants_under_force_rls(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_a = await create_workspace(runtime_session_factory, "RlsA")
    tenant_b = await create_workspace(runtime_session_factory, "RlsB")
    owner_a = await add_member(runtime_session_factory, tenant_a, UserRole.OWNER)
    account_a = await seed_account(runtime_session_factory, tenant_a)

    service = StoreScopeService()
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await service.assign_scope(
            session,
            tenant_a,
            principal(tenant_a, owner_a, UserRole.OWNER),
            StoreScopeAssignRequest(
                user_id=owner_a, channel_account_id=account_a, access_level="manage"
            ),
        )
        await KbGapService().create_redline_rule(
            session,
            tenant_a,
            RedlineRuleCreate(name="No deposits", pattern="deposit", action="block"),
            actor_id=str(owner_a),
        )
        await KbGapService().create_or_bump_kb_gap(
            session, tenant_a, KbGapCreateRequest(customer_question="Do you ship to Chile?")
        )

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        for table in ("member_channel_scopes", "outbound_redline_rules", "kb_gap_items"):
            leaked = await session.scalar(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_a},
            )
            assert int(leaked or 0) == 0, table
        foreign_rows = await session.scalar(text("SELECT COUNT(*) FROM kb_gap_items"))
        assert int(foreign_rows or 0) == 0


# ----------------------------------------------------------------------------
# 2. Store scopes: create / list / delete / RBAC / tenancy validation
# ----------------------------------------------------------------------------


async def test_store_scope_create_list_delete_with_role_matrix(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Scopes")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    manager_id = await add_member(runtime_session_factory, tenant_id, UserRole.MANAGER)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    account_id = await seed_account(runtime_session_factory, tenant_id)
    second_account = await seed_account(
        runtime_session_factory, tenant_id, channel="telegram", display_name="TG hotline"
    )

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, manager_id, UserRole.MANAGER))
    async with harness.client() as client:
        created = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(agent_id),
                "channel_account_id": str(account_id),
                "access_level": "operate",
            },
        )
        assert created.status_code == 201, created.text
        scope = created.json()
        assert scope["access_level"] == "operate"
        assert scope["user_id"] == str(agent_id)
        assert scope["channel_account_id"] == str(account_id)
        assert scope["channel_type"] == "email"

        # Re-assigning the same pair re-levels instead of duplicating.
        relevelled = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(agent_id),
                "channel_account_id": str(account_id),
                "access_level": "manage",
            },
        )
        assert relevelled.status_code == 201, relevelled.text
        assert relevelled.json()["id"] == scope["id"]
        assert relevelled.json()["access_level"] == "manage"

        await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(manager_id),
                "channel_account_id": str(second_account),
                "access_level": "read",
            },
        )

        # Manager sees every grant in the workspace.
        everything = await client.get("/api/v1/tenants/store-scopes")
        assert everything.status_code == 200
        assert len(everything.json()) == 2

        # A member may not grant permissions at all.
        harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
        forbidden = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(agent_id),
                "channel_account_id": str(second_account),
                "access_level": "read",
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "ERR_INSUFFICIENT_ROLE"

        # A member only ever sees their own grants.
        own = await client.get("/api/v1/tenants/store-scopes")
        assert own.status_code == 200
        assert {row["user_id"] for row in own.json()} == {str(agent_id)}

        # ... and cannot ask for someone else's.
        cross = await client.get(
            "/api/v1/tenants/store-scopes", params={"user_id": str(manager_id)}
        )
        assert cross.status_code == 403

        # Revoking is editor-only too; then the owner does it for real.
        assert (
            await client.delete(f"/api/v1/tenants/store-scopes/{scope['id']}")
        ).status_code == 403
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
        removed = await client.delete(f"/api/v1/tenants/store-scopes/{scope['id']}")
        assert removed.status_code == 204
        second_attempt = await client.delete(f"/api/v1/tenants/store-scopes/{scope['id']}")
        assert second_attempt.status_code == 404

        remaining = await client.get("/api/v1/tenants/store-scopes")
        assert [row["user_id"] for row in remaining.json()] == [str(manager_id)]

    actions = await audit_actions(runtime_session_factory, tenant_id)
    assert actions.count(f"{SCOPE_ACTION}.assigned") == 3
    assert actions.count(f"{SCOPE_ACTION}.revoked") == 1


async def test_store_scope_rejects_foreign_member_and_foreign_account(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_a = await create_workspace(runtime_session_factory, "ScopeA")
    tenant_b = await create_workspace(runtime_session_factory, "ScopeB")
    owner_a = await add_member(runtime_session_factory, tenant_a, UserRole.OWNER)
    owner_b = await add_member(runtime_session_factory, tenant_b, UserRole.OWNER)
    account_a = await seed_account(runtime_session_factory, tenant_a)
    account_b = await seed_account(runtime_session_factory, tenant_b)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_a, owner_a, UserRole.OWNER))
    async with harness.client() as client:
        foreign_member = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(owner_b),
                "channel_account_id": str(account_a),
                "access_level": "read",
            },
        )
        assert foreign_member.status_code == 404
        assert foreign_member.json()["code"] == "ERR_UNKNOWN_MEMBER"

        foreign_account = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(owner_a),
                "channel_account_id": str(account_b),
                "access_level": "read",
            },
        )
        assert foreign_account.status_code == 404
        assert foreign_account.json()["code"] == "ERR_UNKNOWN_CHANNEL_ACCOUNT"

        invalid_level = await client.post(
            "/api/v1/tenants/store-scopes",
            json={
                "user_id": str(owner_a),
                "channel_account_id": str(account_a),
                "access_level": "owner",
            },
        )
        assert invalid_level.status_code == 422


# ----------------------------------------------------------------------------
# 3. Offboarding handover: dry-run vs execute
# ----------------------------------------------------------------------------


async def test_handover_dry_run_counts_without_mutating(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "HandoverDry")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    leaver_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    successor_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)

    account = await seed_account(runtime_session_factory, tenant_id, owner_user_id=leaver_id)
    await seed_conversation(runtime_session_factory, tenant_id, account, assigned_user_id=leaver_id)
    await seed_opportunity(runtime_session_factory, tenant_id, leaver_id)
    await seed_opportunity(runtime_session_factory, tenant_id, leaver_id)

    service = StoreScopeService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await service.assign_scope(
            session,
            tenant_id,
            principal(tenant_id, owner_id, UserRole.OWNER),
            StoreScopeAssignRequest(user_id=leaver_id, channel_account_id=account),
        )

    before = await owned_counts(runtime_session_factory, tenant_id, leaver_id)
    assert before == {
        "opportunities": 2,
        "conversations": 1,
        "channel_accounts": 1,
        "store_scopes": 1,
    }

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
    async with harness.client() as client:
        preview = await client.post(
            "/api/v1/tenants/handover",
            json={
                "from_user_id": str(leaver_id),
                "to_user_id": str(successor_id),
                "deactivate_from_user": True,
                "dry_run": True,
            },
        )
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        assert payload["dry_run"] is True
        assert payload["opportunities_reassigned"] == 2
        assert payload["conversations_reassigned"] == 1
        assert payload["channel_accounts_reassigned"] == 1
        assert payload["store_scopes_reassigned"] == 1
        assert payload["total_reassigned"] == 5
        assert payload["from_user_deactivated"] is True

    # Nothing moved, nothing was deactivated, nothing was audited.
    assert await owned_counts(runtime_session_factory, tenant_id, leaver_id) == before
    assert await owned_counts(runtime_session_factory, tenant_id, successor_id) == {
        "opportunities": 0,
        "conversations": 0,
        "channel_accounts": 0,
        "store_scopes": 0,
    }
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        active = await session.scalar(
            text(
                "SELECT is_active FROM tenant_memberships WHERE tenant_id = :tid AND user_id = :uid"
            ),
            {"tid": tenant_id, "uid": leaver_id},
        )
        assert active is True
    actions = await audit_actions(runtime_session_factory, tenant_id)
    assert HANDOVER_ACTION not in actions


async def test_handover_execute_reassigns_everything_and_merges_scope_conflicts(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "HandoverRun")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    leaver_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    successor_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)

    shared_account = await seed_account(
        runtime_session_factory, tenant_id, owner_user_id=leaver_id, display_name="Shared inbox"
    )
    leaver_only_account = await seed_account(
        runtime_session_factory, tenant_id, owner_user_id=leaver_id, display_name="Leaver inbox"
    )
    await seed_conversation(
        runtime_session_factory, tenant_id, shared_account, assigned_user_id=leaver_id
    )
    await seed_opportunity(runtime_session_factory, tenant_id, leaver_id)
    await seed_opportunity(runtime_session_factory, tenant_id, leaver_id)
    await seed_opportunity(runtime_session_factory, tenant_id, leaver_id)

    service = StoreScopeService()
    owner_principal = principal(tenant_id, owner_id, UserRole.OWNER)
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await service.assign_scope(
            session,
            tenant_id,
            owner_principal,
            StoreScopeAssignRequest(
                user_id=leaver_id, channel_account_id=shared_account, access_level="manage"
            ),
        )
        await service.assign_scope(
            session,
            tenant_id,
            owner_principal,
            StoreScopeAssignRequest(
                user_id=leaver_id, channel_account_id=leaver_only_account, access_level="read"
            ),
        )
        # The successor already holds a grant on the shared store: the handover must
        # merge instead of violating uq_member_channel_scopes_unique.
        await service.assign_scope(
            session,
            tenant_id,
            owner_principal,
            StoreScopeAssignRequest(
                user_id=successor_id, channel_account_id=shared_account, access_level="operate"
            ),
        )

    harness = build_harness(runtime_session_factory)
    harness.act_as(owner_principal)
    async with harness.client() as client:
        executed = await client.post(
            "/api/v1/tenants/handover",
            json={
                "from_user_id": str(leaver_id),
                "to_user_id": str(successor_id),
                "deactivate_from_user": True,
                "dry_run": False,
            },
        )
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["dry_run"] is False
        assert payload["opportunities_reassigned"] == 3
        assert payload["conversations_reassigned"] == 1
        assert payload["channel_accounts_reassigned"] == 2
        assert payload["store_scopes_reassigned"] == 2
        assert payload["total_reassigned"] == 8
        assert payload["from_user_deactivated"] is True

    # The leaver owns nothing anymore.
    assert await owned_counts(runtime_session_factory, tenant_id, leaver_id) == {
        "opportunities": 0,
        "conversations": 0,
        "channel_accounts": 0,
        "store_scopes": 0,
    }

    successor = await owned_counts(runtime_session_factory, tenant_id, successor_id)
    assert successor == {
        "opportunities": 3,
        "conversations": 1,
        "channel_accounts": 2,
        "store_scopes": 2,
    }

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        levels = dict(
            (
                await session.execute(
                    text(
                        "SELECT channel_account_id, access_level FROM member_channel_scopes "
                        "WHERE tenant_id = :tid AND user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": successor_id},
                )
            ).all()
        )
        assert levels[shared_account] == "operate"  # the successor's own grant survives
        assert levels[leaver_only_account] == "read"  # the moved grant keeps its level

        deactivated = await session.scalar(
            text(
                "SELECT is_active FROM tenant_memberships WHERE tenant_id = :tid AND user_id = :uid"
            ),
            {"tid": tenant_id, "uid": leaver_id},
        )
        assert deactivated is False

    actions = await audit_actions(runtime_session_factory, tenant_id)
    assert actions.count(HANDOVER_ACTION) == 1

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        after_state: Any = await session.scalar(
            text(
                "SELECT after_state FROM audit_events WHERE tenant_id = :tid AND action = :action"
            ),
            {"tid": tenant_id, "action": HANDOVER_ACTION},
        )
        assert after_state["to_user_id"] == str(successor_id)
        assert after_state["store_scopes_reassigned"] == 2
        assert after_state["from_user_deactivated"] is True


async def test_handover_rejects_manager_role_and_self_target(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "HandoverGuard")
    admin_id = await add_member(runtime_session_factory, tenant_id, UserRole.ADMIN)
    manager_id = await add_member(runtime_session_factory, tenant_id, UserRole.MANAGER)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, manager_id, UserRole.MANAGER))
    async with harness.client() as client:
        blocked = await client.post(
            "/api/v1/tenants/handover",
            json={
                "from_user_id": str(agent_id),
                "to_user_id": str(admin_id),
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    harness.act_as(principal(tenant_id, admin_id, UserRole.ADMIN))
    async with harness.client() as client:
        same_user = await client.post(
            "/api/v1/tenants/handover",
            json={"from_user_id": str(agent_id), "to_user_id": str(agent_id)},
        )
        assert same_user.status_code == 422
        assert same_user.json()["code"] == "ERR_INVALID_HANDOVER_TARGET"

        unknown = await client.post(
            "/api/v1/tenants/handover",
            json={"from_user_id": str(uuid4()), "to_user_id": str(agent_id)},
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "ERR_UNKNOWN_MEMBER"


# ----------------------------------------------------------------------------
# 4. Outbound red-line guard
# ----------------------------------------------------------------------------


async def test_redline_rules_block_and_warn_with_exact_matches(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Redline")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    service = KbGapService()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await service.create_redline_rule(
            session,
            tenant_id,
            RedlineRuleCreate(name="No deposit terms", pattern="deposit", action="block"),
            actor_id=str(owner_id),
        )
        await service.create_redline_rule(
            session,
            tenant_id,
            RedlineRuleCreate(
                name="Guaranteed lead time",
                pattern=r"\bguarantee[ds]?\b",
                match_type="regex",
                action="warn",
            ),
            actor_id=str(owner_id),
        )
        await service.create_redline_rule(
            session,
            tenant_id,
            RedlineRuleCreate(
                name="Disabled rule",
                pattern="discontinued",
                action="block",
                is_enabled=False,
            ),
            actor_id=str(owner_id),
        )

        clean = await service.check_outbound_text(
            session, tenant_id, "We can ship 500 units next week."
        )
        assert clean.blocked is False
        assert clean.blocks == []
        assert clean.warnings == []
        assert clean.matched_rules == []

        warned = await service.check_outbound_text(
            session, tenant_id, "We can ship next week, guaranteed."
        )
        assert warned.blocked is False
        assert warned.warnings == ["guaranteed"]
        assert [rule.action for rule in warned.matched_rules] == ["warn"]

        blocked = await service.check_outbound_text(
            session,
            tenant_id,
            "Please wire the DEPOSIT first; delivery is guaranteed.",
        )
        assert blocked.blocked is True
        assert blocked.blocks == ["deposit"]
        assert blocked.warnings == ["guaranteed"]
        assert {(rule.name, rule.action) for rule in blocked.matched_rules} == {
            ("No deposit terms", "block"),
            ("Guaranteed lead time", "warn"),
        }

        # A disabled rule never fires.
        assert (
            await service.check_outbound_text(session, tenant_id, "This SKU is discontinued.")
        ).blocked is False

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
    async with harness.client() as client:
        listed = await client.get("/api/v1/ai/redline-rules")
        assert listed.status_code == 200
        assert len(listed.json()) == 3

        checked = await client.post(
            "/api/v1/ai/redline-rules/check",
            json={"content": "Deposit required, guaranteed shipping."},
        )
        assert checked.status_code == 200
        body = checked.json()
        assert body["blocked"] is True
        assert body["blocks"] == ["deposit"]
        assert len(body["matched_rules"]) == 2

        # Agents may not author or delete red lines.
        assert (
            await client.post(
                "/api/v1/ai/redline-rules",
                json={"name": "x", "pattern": "y"},
            )
        ).status_code == 403

    harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
    async with harness.client() as client:
        rules = (await client.get("/api/v1/ai/redline-rules")).json()
        target = next(rule for rule in rules if rule["name"] == "No deposit terms")
        assert (await client.delete(f"/api/v1/ai/redline-rules/{target['id']}")).status_code == 204
        assert (await client.delete(f"/api/v1/ai/redline-rules/{target['id']}")).status_code == 404
        reparsed = (await client.get("/api/v1/ai/redline-rules")).json()
        assert {rule["name"] for rule in reparsed} == {
            "Guaranteed lead time",
            "Disabled rule",
        }


async def test_redline_invalid_regex_and_oversized_pattern_return_422(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "RedlineBad")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
    async with harness.client() as client:
        broken = await client.post(
            "/api/v1/ai/redline-rules",
            json={"name": "Broken", "pattern": "(unclosed", "match_type": "regex"},
        )
        assert broken.status_code == 422
        assert broken.json()["code"] == "ERR_INVALID_REDLINE_PATTERN"

        too_long = await client.post(
            "/api/v1/ai/redline-rules",
            json={"name": "Huge", "pattern": "a" * 201, "match_type": "regex"},
        )
        assert too_long.status_code == 422
        assert too_long.json()["code"] == "ERR_INVALID_REDLINE_PATTERN"

        long_keyword = await client.post(
            "/api/v1/ai/redline-rules",
            json={"name": "Huge keyword", "pattern": "k" * 201},
        )
        assert long_keyword.status_code == 422
        assert long_keyword.json()["code"] == "ERR_REDLINE_PATTERN_TOO_LONG"

        assert (await client.get("/api/v1/ai/redline-rules")).json() == []


# ----------------------------------------------------------------------------
# 5. KB gap list, promote-to-KB, dismiss
# ----------------------------------------------------------------------------


async def test_kb_gap_bump_promote_and_searchable_kb_chunk(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Gaps")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    account_id = await seed_account(runtime_session_factory, tenant_id)
    conversation_id = await seed_conversation(runtime_session_factory, tenant_id, account_id)
    service = KbGapService()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        first = await service.create_or_bump_kb_gap(
            session,
            tenant_id,
            KbGapCreateRequest(
                customer_question="Do you ship to Chile?",
                conversation_id=conversation_id,
            ),
        )
        assert first.frequency_count == 1
        assert first.status == "open"
        assert first.conversation_id == conversation_id

        # The same open question bumps the counter instead of duplicating the row.
        bumped = await service.create_or_bump_kb_gap(
            session,
            tenant_id,
            KbGapCreateRequest(customer_question="Do you ship to Chile?"),
        )
        assert bumped.id == first.id
        assert bumped.frequency_count == 2

        await service.create_or_bump_kb_gap(
            session, tenant_id, KbGapCreateRequest(customer_question="Is IP67 available?")
        )

        listed = await service.list_kb_gaps(session, tenant_id, status="open")
        assert listed.total == 2
        assert listed.items[0].customer_question == "Do you ship to Chile?"  # highest frequency

        promoted = await service.promote_kb_gap(
            session,
            tenant_id,
            bumped.id,
            "Yes - we ship DDP to Santiago from Shenzhen with our own freight partner.",
            actor_id=str(owner_id),
        )
        assert promoted.chunks_created == 1
        assert promoted.gap.status == "resolved"
        assert promoted.gap.promoted_kb_document_id == promoted.document_id
        assert promoted.gap.resolved_answer == (
            "Yes - we ship DDP to Santiago from Shenzhen with our own freight partner."
        )

        document = (
            await session.execute(
                text(
                    "SELECT title, source_type, source_uri, language, is_active, metadata_json "
                    "FROM kb_documents WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant_id, "did": promoted.document_id},
            )
        ).one()
        assert document.source_type == "faq_json"
        assert document.source_uri == f"kb-gap:{bumped.id}"
        assert document.is_active is True
        assert document.language == "en"
        assert document.metadata_json["origin"] == "kb_gap"
        assert document.metadata_json["gap_id"] == str(bumped.id)

        chunk = (
            await session.execute(
                text(
                    "SELECT chunk_index, content, token_count, embedding IS NOT NULL AS has_vec, "
                    "has_dynamic_price_disclaimer FROM kb_chunks "
                    "WHERE tenant_id = :tid AND document_id = :did"
                ),
                {"tid": tenant_id, "did": promoted.document_id},
            )
        ).one()
        assert chunk.chunk_index == 0
        assert chunk.content.startswith("Q: Do you ship to Chile?")
        assert "DDP to Santiago" in chunk.content
        assert chunk.token_count > 0
        assert chunk.has_vec is True
        # A pure logistics answer carries no quotable number, so it is stored verbatim.
        assert chunk.has_dynamic_price_disclaimer is False

        # The chunk is really indexed: full-text search finds it, and the HNSW
        # cosine distance of the deterministic embedding is finite.
        hit = await session.scalar(
            text(
                "SELECT content FROM kb_chunks "
                "WHERE tenant_id = :tid AND tsv @@ plainto_tsquery('simple', :q)"
            ),
            {"tid": tenant_id, "q": "Santiago"},
        )
        assert hit is not None
        distance = await session.scalar(
            text(
                "SELECT CAST(embedding <=> CAST(CAST(:emb AS text) AS vector) AS float) "
                "FROM kb_chunks WHERE tenant_id = :tid AND document_id = :did"
            ),
            {
                "tid": tenant_id,
                "did": promoted.document_id,
                "emb": (await _embedding_literal(session, tenant_id, promoted.document_id)),
            },
        )
        assert distance is not None
        assert -0.01 <= float(distance) <= 1.01

        resolved_gaps = await service.list_kb_gaps(session, tenant_id, status="resolved")
        assert resolved_gaps.total == 1

        # A new instance of the same question starts a fresh open gap.
        fresh = await service.create_or_bump_kb_gap(
            session,
            tenant_id,
            KbGapCreateRequest(customer_question="Do you ship to Chile?"),
        )
        assert fresh.id != bumped.id
        assert fresh.frequency_count == 1

    actions = await audit_actions(runtime_session_factory, tenant_id)
    assert actions.count("ai.kb_gap.promoted") == 1


async def test_kb_gap_promotion_redacts_dynamic_price_and_moq_from_the_chunk(
    runtime_session_factory: RuntimeFactory,
) -> None:
    """Zero-hallucination grounding: quotable numbers never enter the vector index."""
    tenant_id = await create_workspace(runtime_session_factory, "GapsRedact")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    service = KbGapService()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        gap = await service.create_or_bump_kb_gap(
            session,
            tenant_id,
            KbGapCreateRequest(customer_question="What is the price and MOQ?"),
        )
        answer = "Unit price is 12.50 USD and MOQ is 200 units."
        promoted = await service.promote_kb_gap(
            session, tenant_id, gap.id, answer, actor_id=str(owner_id)
        )

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT content, has_dynamic_price_disclaimer FROM kb_chunks "
                    "WHERE tenant_id = :tid AND document_id = :did"
                ),
                {"tid": tenant_id, "did": promoted.document_id},
            )
        ).one()
        assert row.has_dynamic_price_disclaimer is True
        assert "12.50" not in row.content
        assert "MOQ is 200" not in row.content
        assert "[PRICE_REDACTED_USE_LIVE_ERP_TOOL]" in row.content

    # The operator's raw answer stays on the gap row for human review.
    assert promoted.gap.resolved_answer == answer


async def _embedding_literal(session: AsyncSession, tenant_id: UUID, document_id: UUID) -> str:
    raw = await session.scalar(
        text(
            "SELECT CAST(embedding AS text) FROM kb_chunks "
            "WHERE tenant_id = :tid AND document_id = :did"
        ),
        {"tid": tenant_id, "did": document_id},
    )
    return str(raw)


async def test_kb_gap_dismiss_and_dismissed_promote_conflict(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "GapsDismiss")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
    async with harness.client() as client:
        created = await client.post(
            "/api/v1/ai/kb-gaps",
            json={"customer_question": "Can you print our logo?", "detected_language": "en"},
        )
        assert created.status_code == 201, created.text
        gap_id = created.json()["id"]

        # Agents cannot dismiss or promote; that is an editor action.
        assert (await client.post(f"/api/v1/ai/kb-gaps/{gap_id}/dismiss")).status_code == 403
        assert (
            await client.post(
                f"/api/v1/ai/kb-gaps/{gap_id}/promote",
                json={"resolved_answer": "Yes, MOQ 500."},
            )
        ).status_code == 403

    harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
    async with harness.client() as client:
        dismissed = await client.post(f"/api/v1/ai/kb-gaps/{gap_id}/dismiss")
        assert dismissed.status_code == 200
        assert dismissed.json()["status"] == "dismissed"

        again = await client.post(f"/api/v1/ai/kb-gaps/{gap_id}/dismiss")
        assert again.status_code == 409
        assert again.json()["code"] == "ERR_KB_GAP_NOT_OPEN"

        promote = await client.post(
            f"/api/v1/ai/kb-gaps/{gap_id}/promote",
            json={"resolved_answer": "Yes, MOQ 500."},
        )
        assert promote.status_code == 409
        assert promote.json()["code"] == "ERR_KB_GAP_DISMISSED"

        missing = await client.post(
            f"/api/v1/ai/kb-gaps/{uuid4()}/promote",
            json={"resolved_answer": "Yes, MOQ 500."},
        )
        assert missing.status_code == 404

        open_gaps = await client.get("/api/v1/ai/kb-gaps", params={"status": "open"})
        assert open_gaps.json()["total"] == 0
        all_gaps = await client.get("/api/v1/ai/kb-gaps")
        assert all_gaps.json()["total"] == 1


# ----------------------------------------------------------------------------
# 6. AI adoption metrics: exact Decimal math
# ----------------------------------------------------------------------------


async def test_ai_adoption_metrics_exact_math(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Adoption")
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    other_tenant = await create_workspace(runtime_session_factory, "AdoptionOther")
    await add_member(runtime_session_factory, other_tenant, UserRole.OWNER)
    await seed_proposal(
        runtime_session_factory,
        other_tenant,
        proposal_type="reply_draft",
        status_value="approved",
    )

    # reply_draft: 3 approved, 1 edited_and_approved, 1 rejected, 2 pending
    for _ in range(3):
        await seed_proposal(
            runtime_session_factory,
            tenant_id,
            proposal_type="reply_draft",
            status_value="approved",
        )
    await seed_proposal(
        runtime_session_factory,
        tenant_id,
        proposal_type="reply_draft",
        status_value="edited_and_approved",
    )
    await seed_proposal(
        runtime_session_factory,
        tenant_id,
        proposal_type="reply_draft",
        status_value="rejected",
    )
    for _ in range(2):
        await seed_proposal(
            runtime_session_factory,
            tenant_id,
            proposal_type="reply_draft",
            status_value="pending",
        )
    # quote_draft: 1 rejected only
    await seed_proposal(
        runtime_session_factory, tenant_id, proposal_type="quote_draft", status_value="rejected"
    )
    # payment_match: 1 expired + 1 failed
    await seed_proposal(
        runtime_session_factory, tenant_id, proposal_type="payment_match", status_value="expired"
    )
    await seed_proposal(
        runtime_session_factory, tenant_id, proposal_type="payment_match", status_value="failed"
    )

    service = KbGapService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        metrics = await service.get_ai_adoption_metrics(session, tenant_id, days=30)

    assert metrics.total == 10
    assert metrics.approved == 3
    assert metrics.edited_and_approved == 1
    assert metrics.rejected == 2
    assert metrics.pending == 2
    assert metrics.expired == 1
    assert metrics.failed == 1
    assert metrics.adopted == 4
    assert metrics.review_base == 6
    # 4/6 = 66.666... -> 66.67 ; 3/6 = 50.00 ; 1/4 = 25.00 ; 2/6 = 33.33
    assert metrics.adoption_rate_pct == Decimal("66.67")
    assert metrics.direct_adopt_rate_pct == Decimal("50.00")
    assert metrics.edit_adopt_rate_pct == Decimal("25.00")
    assert metrics.rejection_rate_pct == Decimal("33.33")

    by_type = {item.proposal_type: item for item in metrics.by_type}
    assert [item.proposal_type for item in metrics.by_type] == [
        "reply_draft",
        "payment_match",
        "quote_draft",
    ]
    assert by_type["reply_draft"].total == 7
    assert by_type["reply_draft"].adoption_rate_pct == Decimal("80.00")  # 4/5
    assert by_type["quote_draft"].adoption_rate_pct == Decimal("0.00")
    assert by_type["payment_match"].adoption_rate_pct == Decimal("0.00")
    assert by_type["payment_match"].pending == 0

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
    async with harness.client() as client:
        response = await client.get("/api/v1/ai/adoption-metrics", params={"days": 30})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 10
        assert body["adoption_rate_pct"] == "66.67"
        assert body["window_days"] == 30
        assert len(body["by_type"]) == 3

        other = await client.get("/api/v1/ai/adoption-metrics", params={"days": 0})
        assert other.status_code == 422


async def test_ai_adoption_metrics_zero_state(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "AdoptionEmpty")
    service = KbGapService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        metrics = await service.get_ai_adoption_metrics(session, tenant_id)
    assert metrics.total == 0
    assert metrics.review_base == 0
    assert metrics.adoption_rate_pct == Decimal("0.00")
    assert metrics.edit_adopt_rate_pct == Decimal("0.00")
    assert metrics.by_type == []
