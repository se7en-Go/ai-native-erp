"""Migration 0009: per-salesperson channel ownership and private customer isolation.

Covers the DB integrity guards, one-paste Telegram connect by an agent, email quick-connect,
anti-hijack 409, agent cross-bind 403, inbound auto-assignment, inbox/SSE isolation,
admin ownership reassignment, and channel disconnect.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.adapters.base import NormalizedInboundEvent
from erp_core.channels.adapters.telegram import telegram_router
from erp_core.channels.adapters.whatsapp import whatsapp_router
from erp_core.channels.ecommerce.base import CredentialVaultHelper, resolve_routing_tenant
from erp_core.channels.inbox_router import inbox_router
from erp_core.channels.inbox_schemas import SendInboxMessageRequest, UpdateConversationRequest
from erp_core.channels.inbox_service import (
    ConversationAccessDeniedError,
    InboxService,
)
from erp_core.channels.ingress_worker import _persist_inbound_event
from erp_core.channels.messaging_accounts import register_messaging_account
from erp_core.channels.settings_router import router as channel_settings_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelType,
    ConversationStatus,
    IdentityType,
    MessageContentType,
    UserRole,
)
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password

RuntimeFactory = async_sessionmaker[AsyncSession]


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


def build_harness(
    factory: RuntimeFactory,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    redis: aioredis.Redis | None = None,
    enqueue: object | None = None,
) -> Harness:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.messaging_http_transport = transport
    if redis is not None:
        app.state.redis = redis
    app.state.outbound_enqueue_fn = enqueue
    app.include_router(telegram_router)
    app.include_router(whatsapp_router)
    app.include_router(channel_settings_router)
    app.include_router(inbox_router)
    harness = Harness(app=app)

    async def _principal() -> CurrentPrincipal:
        return harness.current["principal"]

    app.dependency_overrides[get_current_principal] = _principal
    return harness


def principal(
    tenant_id: UUID, user_id: UUID, role: UserRole, email: str | None = None
) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=email or f"{role.value}-{user_id.hex[:8]}@factory.test",
    )


async def create_workspace(factory: RuntimeFactory) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Ownership Co', :slug)"),
            {"tid": tenant_id, "slug": f"ownership-{tenant_id.hex[:12]}"},
        )
    return tenant_id


async def add_member(
    factory: RuntimeFactory, tenant_id: UUID, role: UserRole
) -> tuple[UUID, str]:
    user_id = uuid4()
    email = f"{role.value}-{user_id.hex[:12]}@ownership.test"
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, :name, :ph)"
            ),
            {
                "uid": user_id,
                "email": email,
                "name": f"{role.value.title()} Person",
                "ph": hash_password("OwnershipPass2026!"),
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, :role)"
            ),
            {"mid": uuid4(), "tid": tenant_id, "uid": user_id, "role": role.value},
        )
    return user_id, email


def telegram_transport(
    bot_id: int, calls: list[tuple[str, dict[str, object]]]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, json.loads(request.content)))
        if method == "getMe":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": bot_id,
                        "is_bot": True,
                        "first_name": "ERP Bot",
                        "username": "OwnershipBot",
                    },
                },
            )
        if method == "setWebhook":
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Telegram method {method}")

    return httpx.MockTransport(handler)


async def seed_account(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    channel: str = "email",
    external: str | None = None,
    display_name: str = "Seed account",
    owner_user_id: UUID | None = None,
    visibility_scope: str = "shared",
    status: str = "active",
) -> UUID:
    aid = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, owner_user_id, visibility_scope) "
                "VALUES (:id, :tid, :channel, :external, :name, :status, :owner, :scope)"
            ),
            {
                "id": aid,
                "tid": tenant_id,
                "channel": channel,
                "external": external or f"seed-{aid.hex[:12]}",
                "name": display_name,
                "status": status,
                "owner": owner_user_id,
                "scope": visibility_scope,
            },
        )
    return aid


async def seed_conversation(
    factory: RuntimeFactory,
    tenant_id: UUID,
    account_id: UUID,
    *,
    channel: str = "email",
    assigned_user_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    customer_id, conv_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2c_consumer', 'Buyer')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, external_thread_id, conversation_mode, status, assigned_user_id, "
                "unread_count) VALUES (:id, :tid, :cid, :aid, :channel, :thread, "
                "'b2c_support', 'open', :assignee, 1)"
            ),
            {
                "id": conv_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": account_id,
                "channel": channel,
                "thread": f"thread-{conv_id.hex[:8]}",
                "assignee": assigned_user_id,
            },
        )
    return conv_id, customer_id


# --------------------------------------------------------------------------------------
# 1. Migration integrity: CHECK constraints + index
# --------------------------------------------------------------------------------------


async def test_migration_0009_constraints_and_index(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    owner_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        indexes = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'channel_accounts'"
                    )
                )
            ).all()
        }
        assert "idx_channel_accounts_tenant_owner" in indexes
        checks = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'channel_accounts'::regclass AND contype = 'c'"
                    )
                )
            ).all()
        }
        assert "ck_channel_accounts_visibility_scope" in checks
        assert "ck_channel_accounts_personal_owner" in checks

    # PERSONAL without an owner is unrepresentable.
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, visibility_scope) "
                    "VALUES (:id, :tid, 'email', 'ownerless@example.test', 'X', 'personal')"
                ),
                {"id": uuid4(), "tid": tenant_id},
            )

    # Unknown scopes are rejected by the database, not just the API.
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, visibility_scope) "
                    "VALUES (:id, :tid, 'email', 'bad-scope@example.test', 'X', 'hidden')"
                ),
                {"id": uuid4(), "tid": tenant_id},
            )

    # Column defaults keep every legacy connector shared and company-owned.
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name) "
                "VALUES (:id, :tid, 'email', 'legacy@example.test', 'Legacy')"
            ),
            {"id": uuid4(), "tid": tenant_id},
        )
        row = (
            await session.execute(
                text(
                    "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND external_account_id = 'legacy@example.test'"
                ),
                {"tid": tenant_id},
            )
        ).one()
        assert row == (None, "shared")

    # Personal rows store the owner and survive an owner deactivation as a FK SET NULL? No:
    # the CHECK constraint keeps the account valid, so deletion of the owner is blocked by
    # the personal-owner CHECK, while an inactive membership leaves the row untouched.
    personal_id = await seed_account(
        runtime_session_factory,
        tenant_id,
        external="personal@example.test",
        owner_user_id=owner_id,
        visibility_scope="personal",
    )
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        stored = (
            await session.execute(
                text(
                    "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": tenant_id, "aid": personal_id},
            )
        ).one()
        assert stored == (owner_id, "personal")


# --------------------------------------------------------------------------------------
# 2. Telegram one-paste connect, agent self-bind, and hijack protection
# --------------------------------------------------------------------------------------


async def test_telegram_one_paste_agent_bind_and_anti_hijack(
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    agent_b_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    manager_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.MANAGER)

    monkeypatch.setenv("TELEGRAM_WEBHOOK_BASE_URL", "https://erp.example.test")
    bot_id = int(str(uuid4().int)[:12])
    token = f"{bot_id}:test-bot-token-xxxxxxxxxx"
    calls: list[tuple[str, dict[str, object]]] = []
    harness = build_harness(
        runtime_session_factory, transport=telegram_transport(bot_id, calls)
    )

    async with harness.client() as client:
        # A sales agent binds a bot with a SINGLE paste: no display name is required.
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT))
        first = await client.post(
            "/api/v1/channels/telegram/connect", json={"bot_token": token}
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["external_account_id"] == str(bot_id)
        assert body["display_name"] == "ERP Bot"
        assert body["owner_user_id"] == str(agent_a_id)
        assert body["visibility_scope"] == "personal"
        assert body["bot_username"] == "OwnershipBot"
        assert token not in first.text

        # Agent B cannot hijack agent A's bot, nor ask for a shared/foreign-owned account.
        harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
        hijack = await client.post(
            "/api/v1/channels/telegram/connect", json={"bot_token": token}
        )
        assert hijack.status_code == 409, hijack.text
        assert hijack.json()["code"] == "ERR_CHANNEL_ROUTING_CONFLICT"
        shared = await client.post(
            "/api/v1/channels/telegram/connect",
            json={"bot_token": token, "visibility_scope": "shared"},
        )
        assert shared.status_code == 403 and shared.json()["code"] == "ERR_INSUFFICIENT_ROLE"
        foreign_owner = await client.post(
            "/api/v1/channels/telegram/connect",
            json={"bot_token": token, "owner_user_id": str(agent_a_id)},
        )
        assert foreign_owner.status_code == 403
        assert foreign_owner.json()["code"] == "ERR_INSUFFICIENT_ROLE"

        # A manager cannot bind a company channel either, but may self-bind a personal one.
        harness.act_as(principal(tenant_id, manager_id, UserRole.MANAGER))
        manager_shared = await client.post(
            "/api/v1/channels/telegram/connect",
            json={"bot_token": token, "visibility_scope": "shared"},
        )
        assert manager_shared.status_code == 403

        # Admins may reassign the account to another active member.
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        reassigned = await client.post(
            "/api/v1/channels/telegram/connect",
            json={
                "bot_token": token,
                "display_name": "Reassigned Bot",
                "owner_user_id": str(agent_b_id),
            },
        )
        assert reassigned.status_code == 200, reassigned.text
        assert reassigned.json()["owner_user_id"] == str(agent_b_id)
        assert reassigned.json()["visibility_scope"] == "personal"

        # An admin can return it to company scope; the stored scope flips back to shared.
        shared_again = await client.post(
            "/api/v1/channels/telegram/connect",
            json={"bot_token": token, "display_name": "Company Bot"},
        )
        assert shared_again.status_code == 200, shared_again.text
        assert shared_again.json()["owner_user_id"] is None
        assert shared_again.json()["visibility_scope"] == "shared"

        # An unknown-but-active-looking owner must be rejected with 422, not silently bound.
        unknown_owner = await client.post(
            "/api/v1/channels/telegram/connect",
            json={"bot_token": token, "owner_user_id": str(uuid4())},
        )
        assert unknown_owner.status_code == 422
        assert unknown_owner.json()["code"] == "ERR_INVALID_CHANNEL_CONNECTION"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, owner_user_id, visibility_scope, config->>'bot_username' "
                    "FROM channel_accounts WHERE tenant_id = :tid AND external_account_id = :ext"
                ),
                {"tid": tenant_id, "ext": str(bot_id)},
            )
        ).one()
        assert row[1] is None and row[2] == "shared" and row[3] == "OwnershipBot"
        credentials, _ = await CredentialVaultHelper().load_secret_json(
            session, tenant_id, row[0], "messaging_provider"
        )
        assert credentials["bot_token"] == token
        assert await resolve_routing_tenant(session, "telegram", str(bot_id)) == (
            tenant_id,
            row[0],
        )
    # Three successful binds: agent A self-bind, admin reassign to B, admin back to shared.
    assert [method for method, _ in calls].count("setWebhook") == 3


# --------------------------------------------------------------------------------------
# 3. Email quick-connect (single-paste, encrypted credentials, agent self-bind)
# --------------------------------------------------------------------------------------


async def test_email_quick_connect_encrypts_secrets_and_scopes_agent(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    harness = build_harness(runtime_session_factory)
    domain = f"factory-{uuid4().hex[:8]}.example.com"
    mixed_case_mailbox = f"Sales.Agent@{domain.upper()}"

    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
        connected = await client.post(
            "/api/v1/channels/email/connect",
            json={
                "email_address": mixed_case_mailbox,
                "provider": "resend",
                "api_key": "re_secret_key_value",
            },
        )
        assert connected.status_code == 200, connected.text
        body = connected.json()
        assert body["external_account_id"] == f"sales.agent@{domain}"
        assert body["display_name"] == f"sales.agent@{domain}"
        assert body["owner_user_id"] == str(agent_id)
        assert body["visibility_scope"] == "personal"
        assert "re_secret_key_value" not in connected.text

        # Missing provider secret fails loudly and never echoes input.
        missing_secret = await client.post(
            "/api/v1/channels/email/connect",
            json={"email_address": f"no-key@{domain}", "provider": "resend"},
        )
        assert missing_secret.status_code == 422
        assert missing_secret.json()["code"] == "ERR_INVALID_CHANNEL_CONNECTION"

        # SMTP/IMAP variant keeps credentials in the vault, never in config or the response.
        smtp = await client.post(
            "/api/v1/channels/email/connect",
            json={
                "email_address": f"b2b@{domain}",
                "display_name": "B2B Desk",
                "provider": "smtp_imap",
                "smtp_host": "smtp.factory.example.com",
                "smtp_port": 587,
                "smtp_username": f"b2b@{domain}",
                "smtp_password": "smtp-secret-value",
                "imap_host": "imap.factory.example.com",
                "imap_port": 993,
            },
        )
        assert smtp.status_code == 200, smtp.text
        assert "smtp-secret-value" not in smtp.text

        # Only an admin may create a company (shared) mailbox.
        agent_shared = await client.post(
            "/api/v1/channels/email/connect",
            json={
                "email_address": f"support@{domain}",
                "provider": "resend",
                "api_key": "re_shared_key",
                "visibility_scope": "shared",
            },
        )
        assert agent_shared.status_code == 403
        assert agent_shared.json()["code"] == "ERR_INSUFFICIENT_ROLE"

        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        company = await client.post(
            "/api/v1/channels/email/connect",
            json={
                "email_address": f"support@{domain}",
                "provider": "resend",
                "api_key": "re_shared_key",
                "display_name": "Company Support",
            },
        )
        assert company.status_code == 200, company.text
        assert company.json()["visibility_scope"] == "shared"
        assert company.json()["owner_user_id"] is None

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, external_account_id, config, owner_user_id, visibility_scope "
                    "FROM channel_accounts WHERE tenant_id = :tid ORDER BY external_account_id"
                ),
                {"tid": tenant_id},
            )
        ).all()
        assert len(rows) == 3
        config_blob = json.dumps(
            [
                {
                    "config": row.config,
                    "owner": str(row.owner_user_id) if row.owner_user_id else None,
                    "scope": row.visibility_scope,
                }
                for row in rows
            ]
        )
        for secret in ("re_secret_key_value", "smtp-secret-value", "re_shared_key"):
            assert secret not in config_blob

        vault = CredentialVaultHelper()
        by_email = {row.external_account_id: row for row in rows}
        agent_creds, _ = await vault.load_secret_json(
            session, tenant_id, by_email[f"sales.agent@{domain}"].id,
            "messaging_provider",
        )
        assert agent_creds == {"api_key": "re_secret_key_value"}
        smtp_creds, _ = await vault.load_secret_json(
            session, tenant_id, by_email[f"b2b@{domain}"].id, "messaging_provider"
        )
        assert smtp_creds == {
            "smtp_password": "smtp-secret-value",
            "smtp_username": f"b2b@{domain}",
        }
        smtp_config = by_email[f"b2b@{domain}"].config
        assert smtp_config["provider"] == "smtp_imap"
        assert smtp_config["smtp_host"] == "smtp.factory.example.com"
        assert smtp_config["imap_port"] == 993
        assert by_email[f"support@{domain}"].visibility_scope == "shared"
        assert by_email[f"support@{domain}"].owner_user_id is None


# --------------------------------------------------------------------------------------
# 4. Accounts listing scoping + ownership reassignment
# --------------------------------------------------------------------------------------


async def test_accounts_listing_scope_and_admin_ownership_reassignment(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id, agent_a_email = await add_member(
        runtime_session_factory, tenant_id, UserRole.AGENT
    )
    agent_b_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    manager_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.MANAGER)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    shared_id = await seed_account(
        runtime_session_factory, tenant_id, external="company@factory.test", display_name="Company"
    )
    personal_a = await seed_account(
        runtime_session_factory,
        tenant_id,
        external="a@factory.test",
        display_name="Agent A mailbox",
        owner_user_id=agent_a_id,
        visibility_scope="personal",
    )
    personal_b = await seed_account(
        runtime_session_factory,
        tenant_id,
        external="b@factory.test",
        display_name="Agent B mailbox",
        owner_user_id=agent_b_id,
        visibility_scope="personal",
    )
    # A personal-scope row owned by someone else is never visible to another agent.
    harness = build_harness(runtime_session_factory)

    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT, agent_a_email))
        agent_view = await client.get("/api/v1/channels/accounts")
        assert agent_view.status_code == 200, agent_view.text
        assert {row["id"] for row in agent_view.json()["items"]} == {
            str(shared_id),
            str(personal_a),
        }
        owned = agent_view.json()["items"]
        mine = next(row for row in owned if row["id"] == str(personal_a))
        assert mine["owner_user_id"] == str(agent_a_id)
        assert mine["owner_email"] == agent_a_email
        assert mine["owner_name"] == "Agent Person"
        assert mine["visibility_scope"] == "personal"
        company = next(row for row in owned if row["id"] == str(shared_id))
        assert company["visibility_scope"] == "shared" and company["owner_user_id"] is None
        assert "config" not in company and "bot_username" in company

        filtered = await client.get("/api/v1/channels/accounts?visibility_scope=personal")
        assert {row["id"] for row in filtered.json()["items"]} == {str(personal_a)}
        by_owner = await client.get(
            f"/api/v1/channels/accounts?owner_user_id={agent_b_id}"
        )
        assert by_owner.json()["total"] == 0

        harness.act_as(principal(tenant_id, manager_id, UserRole.MANAGER))
        manager_view = await client.get("/api/v1/channels/accounts")
        assert manager_view.json()["total"] == 3

        # Managers still cannot reassign ownership or disconnect company channels.
        manager_patch = await client.patch(
            f"/api/v1/channels/accounts/{shared_id}/ownership",
            json={"visibility_scope": "personal", "owner_user_id": str(agent_a_id)},
        )
        assert manager_patch.status_code == 403
        manager_delete = await client.delete(f"/api/v1/channels/accounts/{shared_id}")
        assert manager_delete.status_code == 403

        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        conv_id, _ = await seed_conversation(
            runtime_session_factory, tenant_id, shared_id, channel="email"
        )
        reassigned = await client.patch(
            f"/api/v1/channels/accounts/{shared_id}/ownership",
            json={
                "visibility_scope": "personal",
                "owner_user_id": str(agent_b_id),
                "reassign_open_conversations": True,
            },
        )
        assert reassigned.status_code == 200, reassigned.text
        assert reassigned.json()["owner_user_id"] == str(agent_b_id)
        assert reassigned.json()["visibility_scope"] == "personal"

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            assignee = await session.scalar(
                text(
                    "SELECT assigned_user_id FROM conversations "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {"tid": tenant_id, "cid": conv_id},
            )
            assert assignee == agent_b_id
            audit_actions = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT action FROM audit_events WHERE tenant_id = :tid "
                            "AND action = 'channel.ownership_updated'"
                        ),
                        {"tid": tenant_id},
                    )
                ).all()
            }
            assert audit_actions == {"channel.ownership_updated"}

        # Reverting to shared clears the owner and only the named conversations get moved.
        cleared = await client.patch(
            f"/api/v1/channels/accounts/{personal_b}/ownership",
            json={"visibility_scope": "shared"},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["owner_user_id"] is None
        assert cleared.json()["visibility_scope"] == "shared"

        # Personal scope without an owner, or with a non-member owner, is a 422.
        no_owner = await client.patch(
            f"/api/v1/channels/accounts/{personal_a}/ownership",
            json={"visibility_scope": "personal"},
        )
        assert no_owner.status_code == 422
        outsider = await client.patch(
            f"/api/v1/channels/accounts/{personal_a}/ownership",
            json={"visibility_scope": "personal", "owner_user_id": str(uuid4())},
        )
        assert outsider.status_code == 422
        missing = await client.patch(
            f"/api/v1/channels/accounts/{uuid4()}/ownership",
            json={"visibility_scope": "shared"},
        )
        assert missing.status_code == 404


# --------------------------------------------------------------------------------------
# 5. Inbound auto-assignment + strict inbox and SSE isolation
# --------------------------------------------------------------------------------------


def inbound_event(account: str, peer: str, mid: str) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        external_event_id=f"evt-{mid}",
        external_account_id=account,
        external_message_id=mid,
        external_thread_id=peer,
        sender_identity_type=IdentityType.EMAIL,
        sender_identity_value=f"{peer}@buyer.test",
        sender_display_name="Buyer",
        body_text="Need a quote",
        content_type=MessageContentType.TEXT,
        source_timestamp=datetime.now(UTC),
    )


async def test_inbound_auto_assignment_and_agent_isolation(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    agent_b_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    personal_a = await seed_account(
        runtime_session_factory,
        tenant_id,
        external="private-a@factory.test",
        display_name="Private A",
        owner_user_id=agent_a_id,
        visibility_scope="personal",
    )
    shared = await seed_account(
        runtime_session_factory, tenant_id, external="company@factory.test", display_name="Company"
    )

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        owned_conv, _ = await _persist_inbound_event(
            session, tenant_id, personal_a, "email", inbound_event(
                "private-a@factory.test", "alice", "msg-private"
            )
        )
        shared_conv, _ = await _persist_inbound_event(
            session, tenant_id, shared, "email", inbound_event(
                "company@factory.test", "bob", "msg-shared"
            )
        )
        assigned = dict(
            (
                await session.execute(
                    text(
                        "SELECT id, assigned_user_id FROM conversations "
                        "WHERE tenant_id = :tid AND id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": [owned_conv, shared_conv]},
                )
            ).all()
        )
    # Inbound traffic on a personal channel auto-assigns to its owner; shared stays unowned.
    assert assigned[owned_conv] == agent_a_id
    assert assigned[shared_conv] is None

    service = InboxService()
    redis = aioredis.from_url("redis://localhost:6379/0")
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            agent_b_view = await service.list_conversations(
                session,
                tenant_id,
                principal=principal(tenant_id, agent_b_id, UserRole.AGENT),
            )
            assert [c.id for c in agent_b_view.items] == [shared_conv]
            assert agent_b_view.items[0].channel_visibility_scope == "shared"
            assert agent_b_view.items[0].channel_account_display_name == "Company"
            assert agent_b_view.items[0].assigned_user_name is None

            agent_a_view = await service.list_conversations(
                session,
                tenant_id,
                principal=principal(tenant_id, agent_a_id, UserRole.AGENT),
            )
            assert {c.id for c in agent_a_view.items} == {owned_conv, shared_conv}
            private_row = next(c for c in agent_a_view.items if c.id == owned_conv)
            assert private_row.channel_visibility_scope == "personal"
            assert private_row.channel_owner_user_id == agent_a_id
            assert private_row.assigned_user_name == "Agent Person"

            owner_view = await service.list_conversations(
                session,
                tenant_id,
                principal=principal(tenant_id, owner_id, UserRole.OWNER, owner_email),
            )
            assert {c.id for c in owner_view.items} == {owned_conv, shared_conv}

            # Agent B cannot read, write, or re-assign agent A's private conversation.
            with pytest.raises(ConversationAccessDeniedError):
                await service.list_messages(
                    session, redis, tenant_id, owned_conv,
                    principal=principal(tenant_id, agent_b_id, UserRole.AGENT),
                )
            await session.rollback()
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            with pytest.raises(ConversationAccessDeniedError):
                await service.send_message(
                    session, redis, tenant_id,
                    principal(tenant_id, agent_b_id, UserRole.AGENT),
                    owned_conv,
                    SendInboxMessageRequest(body_text="hijack attempt"),
                )
            await session.rollback()
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            with pytest.raises(ConversationAccessDeniedError):
                await service.update_conversation(
                    session, redis, tenant_id,
                    principal(tenant_id, agent_b_id, UserRole.AGENT),
                    owned_conv,
                    UpdateConversationRequest(status=ConversationStatus.RESOLVED),
                )
            await session.rollback()

        # Agent B may work the shared conversation but never reassign it to someone else.
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            notes = await service.list_messages(
                session, redis, tenant_id, shared_conv,
                principal=principal(tenant_id, agent_b_id, UserRole.AGENT),
            )
            assert notes.total == 1
            with pytest.raises(ConversationAccessDeniedError):
                await service.update_conversation(
                    session, redis, tenant_id,
                    principal(tenant_id, agent_b_id, UserRole.AGENT),
                    shared_conv,
                    UpdateConversationRequest(assigned_user_id=agent_a_id),
                )
            await session.rollback()

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            claimed = await service.update_conversation(
                session, redis, tenant_id,
                principal(tenant_id, agent_b_id, UserRole.AGENT),
                shared_conv,
                UpdateConversationRequest(assigned_user_id=agent_b_id),
            )
            assert claimed.assigned_user_id == agent_b_id
    finally:
        await redis.aclose()

    # HTTP surface: list/messages/patch all reject the foreign private conversation.
    harness = build_harness(runtime_session_factory, redis=aioredis.from_url(
        "redis://localhost:6379/0"
    ))
    try:
        async with harness.client() as client:
            harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
            listing = await client.get("/api/v1/inbox/conversations")
            assert listing.status_code == 200, listing.text
            assert [row["id"] for row in listing.json()["items"]] == [str(shared_conv)]
            denied_messages = await client.get(
                f"/api/v1/inbox/conversations/{owned_conv}/messages"
            )
            assert denied_messages.status_code == 403
            assert denied_messages.json()["detail"] == "ERR_CONVERSATION_ACCESS_DENIED"
            denied_send = await client.post(
                f"/api/v1/inbox/conversations/{owned_conv}/messages",
                json={"body_text": "nope"},
            )
            assert denied_send.status_code == 403
            denied_patch = await client.patch(
                f"/api/v1/inbox/conversations/{owned_conv}",
                json={"status": "resolved"},
            )
            assert denied_patch.status_code == 403

            # SSE privacy filter: foreign private events are dropped, shared events pass.
            sse_redis = harness.app.state.redis
            channel = f"inbox:events:{tenant_id}"

            async def _publish_once_subscribed() -> None:
                for _ in range(250):
                    counts = await sse_redis.pubsub_numsub(channel)
                    if counts and counts[0][1] >= 1:
                        break
                    await asyncio.sleep(0.02)
                await sse_redis.publish(
                    channel,
                    json.dumps(
                        {
                            "event": "message.created",
                            "conversation_id": str(owned_conv),
                            "assigned_user_id": str(agent_a_id),
                            "visibility_scope": "personal",
                        }
                    ),
                )
                await sse_redis.publish(
                    channel,
                    json.dumps(
                        {
                            "event": "message.created",
                            "conversation_id": str(shared_conv),
                            "assigned_user_id": None,
                            "visibility_scope": "shared",
                        }
                    ),
                )

            pub = asyncio.create_task(_publish_once_subscribed())
            try:
                stream = await asyncio.wait_for(
                    client.get("/api/v1/inbox/stream?max_events=1"), timeout=10.0
                )
            finally:
                await pub
            assert stream.status_code == 200
            data_lines = [
                line for line in stream.text.splitlines() if line.startswith("data: ")
            ]
            payloads = [json.loads(line.removeprefix("data: ")) for line in data_lines]
            conversations = [
                payload.get("conversation_id")
                for payload in payloads
                if "conversation_id" in payload
            ]
            assert str(owned_conv) not in conversations
            assert str(shared_conv) in conversations
    finally:
        await harness.app.state.redis.aclose()


async def test_sse_filter_allows_own_private_and_legacy_events(
    runtime_session_factory: RuntimeFactory,
) -> None:
    from erp_core.channels.inbox_service import event_visible_to_agent

    tenant_id = uuid4()
    agent_id = uuid4()
    other_id = uuid4()
    agent = principal(tenant_id, agent_id, UserRole.AGENT)
    assert event_visible_to_agent(agent, {"visibility_scope": "personal",
                                          "assigned_user_id": str(agent_id)})
    assert not event_visible_to_agent(agent, {"visibility_scope": "personal",
                                              "assigned_user_id": str(other_id)})
    assert not event_visible_to_agent(agent, {"conversation_id": "c",
                                              "assigned_user_id": str(other_id)})
    assert event_visible_to_agent(agent, {"conversation_id": "c",
                                          "visibility_scope": "shared",
                                          "assigned_user_id": None})
    # Legacy/tenant-wide events without ownership metadata stay visible to agents.
    assert event_visible_to_agent(agent, {"event": "proposal.created"})
    owner = principal(tenant_id, other_id, UserRole.OWNER)
    assert event_visible_to_agent(owner, {"visibility_scope": "personal",
                                          "assigned_user_id": str(other_id)})


# --------------------------------------------------------------------------------------
# 6. Disconnect: agent-owned only, routing deactivated, admin can always disconnect
# --------------------------------------------------------------------------------------


async def test_disconnect_deactivates_routing_and_respects_ownership(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id, agent_a_email = await add_member(
        runtime_session_factory, tenant_id, UserRole.AGENT
    )
    agent_b_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    domain = f"disconnect-{uuid4().hex[:8]}.example.com"
    agent_mailbox = f"agent-a@{domain}"
    company_mailbox = f"company@{domain}"
    personal_a = await seed_account(
        runtime_session_factory,
        tenant_id,
        external=agent_mailbox,
        display_name="Agent A mailbox",
        owner_user_id=agent_a_id,
        visibility_scope="personal",
    )
    shared = await seed_account(
        runtime_session_factory,
        tenant_id,
        external=company_mailbox,
        display_name="Company",
    )
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        from erp_core.channels.ecommerce.base import register_channel_routing

        for aid, ext in (
            (personal_a, agent_mailbox),
            (shared, company_mailbox),
        ):
            await register_channel_routing(
                session,
                channel_type="email",
                external_account_id=ext,
                tenant_id=tenant_id,
                channel_account_id=aid,
            )
        assert await resolve_routing_tenant(session, "email", agent_mailbox) == (
            tenant_id,
            personal_a,
        )

    harness = build_harness(runtime_session_factory)
    async with harness.client() as client:
        # Agent B cannot disconnect agent A's personal channel or a company channel.
        harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
        foreign = await client.delete(f"/api/v1/channels/accounts/{personal_a}")
        assert foreign.status_code == 403
        assert foreign.json()["detail"] == "ERR_INSUFFICIENT_ROLE"
        company = await client.delete(f"/api/v1/channels/accounts/{shared}")
        assert company.status_code == 403

        # The owner of a personal channel may disconnect it.
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT, agent_a_email))
        disconnected = await client.delete(f"/api/v1/channels/accounts/{personal_a}")
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json() == {
            "channel_account_id": str(personal_a),
            "status": "disconnected",
        }

        # Owners/admins may always disconnect company channels.
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        company_disconnect = await client.delete(f"/api/v1/channels/accounts/{shared}")
        assert company_disconnect.status_code == 200

        missing = await client.delete(f"/api/v1/channels/accounts/{uuid4()}")
        assert missing.status_code == 404

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        statuses = dict(
            (
                await session.execute(
                    text(
                        "SELECT id, status FROM channel_accounts "
                        "WHERE tenant_id = :tid AND id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": [personal_a, shared]},
                )
            ).all()
        )
        assert statuses == {personal_a: "disconnected", shared: "disconnected"}
        # Inactive routing means signed provider webhooks no longer reach this tenant.
        assert await resolve_routing_tenant(session, "email", agent_mailbox) is None
        assert await resolve_routing_tenant(session, "email", company_mailbox) is None
        audit_count = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE tenant_id = :tid "
                "AND action = 'channel.disconnected'"
            ),
            {"tid": tenant_id},
        )
        assert audit_count == 2

        # Reconnecting the same address reactivates the account and its routing.
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            reactivated = await register_messaging_account(
                session,
                tenant_id,
                ChannelType.EMAIL,
                agent_mailbox,
                "Agent A mailbox",
                owner_user_id=agent_a_id,
                visibility_scope="personal",
                actor_user_id=agent_a_id,
                actor_is_admin=False,
            )
            assert reactivated == personal_a
            row = (
                await session.execute(
                    text(
                        "SELECT status, owner_user_id, visibility_scope FROM "
                        "channel_accounts WHERE tenant_id = :tid AND id = :aid"
                    ),
                    {"tid": tenant_id, "aid": personal_a},
                )
            ).one()
            assert row == ("active", agent_a_id, "personal")
            assert await resolve_routing_tenant(session, "email", agent_mailbox) == (
                tenant_id,
                personal_a,
            )


# --------------------------------------------------------------------------------------
# 7. WhatsApp: Coexistence numbers self-bind for sales, official WABA stays admin-only
# --------------------------------------------------------------------------------------


async def test_whatsapp_coexistence_self_bind_but_cloud_requires_admin(
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_APP_ID", "meta_app_ownership")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "meta_secret_ownership")
    from erp_core.channels.adapters import whatsapp as whatsapp_module

    tenant_id = await create_workspace(runtime_session_factory)
    agent_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    phone_number_id = f"pn_{uuid4().hex[:10]}"
    waba_id = f"waba_{uuid4().hex[:10]}"

    def graph(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth/access_token" in url:
            return httpx.Response(200, json={"access_token": "EAAG_OWNERSHIP"})
        if "/subscribed_apps" in url:
            return httpx.Response(200, json={"success": True})
        if "fields=is_on_biz_app" in url or f"/{phone_number_id}?" in url:
            return httpx.Response(200, json={"is_on_biz_app": True, "platform_type": "CLOUD_API"})
        if "/smb_app_data" in url:
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": "unhandled"})

    transport = httpx.MockTransport(graph)
    real_adapter = whatsapp_module.WhatsAppAdapter

    def adapter_factory(**kwargs: object) -> object:
        kwargs.setdefault("http_transport", transport)
        return real_adapter(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(whatsapp_module, "WhatsAppAdapter", adapter_factory)
    harness = build_harness(runtime_session_factory)

    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
        # Official Cloud API WABA onboarding remains admin-only.
        cloud = await client.post(
            "/api/v1/channels/whatsapp/embedded-signup",
            json={
                "code": "oauth_code_agent",
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "is_coexistence": False,
            },
        )
        assert cloud.status_code == 403, cloud.text
        assert cloud.json()["code"] == "ERR_INSUFFICIENT_ROLE"

        # Coexistence self-service: a salesperson may bind their own business number.
        coexistence = await client.post(
            "/api/v1/channels/whatsapp/embedded-signup",
            json={
                "code": "oauth_code_agent",
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "is_coexistence": True,
                "display_name": "Rep Mobile",
            },
        )
        assert coexistence.status_code == 200, coexistence.text
        assert coexistence.json()["channel_type"] == "whatsapp_coexistence"

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                        "WHERE tenant_id = :tid AND external_account_id = :ext"
                    ),
                    {"tid": tenant_id, "ext": phone_number_id},
                )
            ).one()
            assert row == (agent_id, "personal")

        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        company = await client.post(
            "/api/v1/channels/whatsapp/embedded-signup",
            json={
                "code": "oauth_code_owner",
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "is_coexistence": True,
                "display_name": "Company Number",
                "visibility_scope": "shared",
            },
        )
        assert company.status_code == 200, company.text

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid AND external_account_id = :ext"
                ),
                {"tid": tenant_id, "ext": phone_number_id},
            )
        ).one()
        assert row == (None, "shared")


# --------------------------------------------------------------------------------------
# 8. WhatsApp Direct Token Quick-Connect (Cloud API admin-only, Coexistence self-bind)
# --------------------------------------------------------------------------------------


async def test_whatsapp_direct_connect_scopes_rate_limits_and_encrypts_token(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    agent_b_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    coexistence_phone = f"pn_{uuid4().hex[:10]}"
    cloud_phone = f"pn_{uuid4().hex[:10]}"
    waba_id = f"waba_{uuid4().hex[:10]}"
    agent_token = "EAAG_agent_coexistence_token_2026"
    owner_token = "EAAG_owner_cloud_token_2026"
    harness = build_harness(runtime_session_factory)

    async with harness.client() as client:
        # A salesperson self-binds their own Coexistence number without any admin help.
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT))
        coexistence = await client.post(
            "/api/v1/channels/whatsapp/connect",
            json={
                "waba_id": waba_id,
                "phone_number_id": coexistence_phone,
                "access_token": agent_token,
                "is_coexistence": True,
            },
        )
        assert coexistence.status_code == 200, coexistence.text
        coexistence_body = coexistence.json()
        assert coexistence_body["external_account_id"] == coexistence_phone
        assert coexistence_body["display_name"] == f"WhatsApp Coexistence ({coexistence_phone})"
        assert coexistence_body["owner_user_id"] == str(agent_a_id)
        assert coexistence_body["visibility_scope"] == "personal"
        assert agent_token not in coexistence.text

        # Official Cloud API onboarding stays admin-only.
        cloud_denied = await client.post(
            "/api/v1/channels/whatsapp/connect",
            json={
                "waba_id": waba_id,
                "phone_number_id": cloud_phone,
                "access_token": agent_token,
                "is_coexistence": False,
            },
        )
        assert cloud_denied.status_code == 403, cloud_denied.text
        assert cloud_denied.json()["code"] == "ERR_INSUFFICIENT_ROLE"

        # A peer salesperson can never hijack the same phone number id.
        harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
        hijack = await client.post(
            "/api/v1/channels/whatsapp/connect",
            json={
                "waba_id": waba_id,
                "phone_number_id": coexistence_phone,
                "access_token": agent_token,
                "is_coexistence": True,
            },
        )
        assert hijack.status_code == 409, hijack.text
        assert hijack.json()["code"] == "ERR_CHANNEL_ROUTING_CONFLICT"

        # An oversized token is rejected without ever echoing the credential.
        oversized_token = "EAAG" + "x" * 2100
        bad_token = await client.post(
            "/api/v1/channels/whatsapp/connect",
            json={
                "waba_id": waba_id,
                "phone_number_id": f"pn_{uuid4().hex[:10]}",
                "access_token": oversized_token,
                "is_coexistence": True,
            },
        )
        assert bad_token.status_code == 422, bad_token.text
        assert bad_token.json()["code"] == "ERR_INVALID_CHANNEL_CONNECTION"
        assert oversized_token not in bad_token.text
        assert "x" * 64 not in bad_token.text

        # Owners bind the official Cloud API number as a shared company channel.
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        cloud = await client.post(
            "/api/v1/channels/whatsapp/connect",
            json={
                "waba_id": waba_id,
                "phone_number_id": cloud_phone,
                "access_token": owner_token,
                "is_coexistence": False,
            },
        )
        assert cloud.status_code == 200, cloud.text
        cloud_body = cloud.json()
        assert cloud_body["external_account_id"] == cloud_phone
        assert cloud_body["display_name"] == f"WhatsApp Cloud ({cloud_phone})"
        assert cloud_body["owner_user_id"] is None
        assert cloud_body["visibility_scope"] == "shared"
        assert owner_token not in cloud.text

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, channel_type, external_account_id, config, rate_limit_profile, "
                    "owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid ORDER BY external_account_id"
                ),
                {"tid": tenant_id},
            )
        ).all()
        assert len(rows) == 2
        by_phone = {row.external_account_id: row for row in rows}

        coexistence_row = by_phone[coexistence_phone]
        assert coexistence_row.channel_type == "whatsapp_coexistence"
        assert coexistence_row.rate_limit_profile["mps"] == 20
        assert coexistence_row.rate_limit_profile["coexistence"] is True
        assert coexistence_row.owner_user_id == agent_a_id
        assert coexistence_row.visibility_scope == "personal"
        assert coexistence_row.config["waba_id"] == waba_id
        assert coexistence_row.config["phone_number_id"] == coexistence_phone
        assert agent_token not in json.dumps(coexistence_row.config)

        cloud_row = by_phone[cloud_phone]
        assert cloud_row.channel_type == "whatsapp_cloud"
        assert cloud_row.rate_limit_profile["mps"] == 80
        assert cloud_row.rate_limit_profile["coexistence"] is False
        assert cloud_row.owner_user_id is None
        assert cloud_row.visibility_scope == "shared"
        assert owner_token not in json.dumps(cloud_row.config)

        # Both tokens live only in the envelope-encrypted vault under the adapter's type.
        for row, expected_token in (
            (coexistence_row, agent_token),
            (cloud_row, owner_token),
        ):
            credentials, _ = await CredentialVaultHelper().load_secret_json(
                session, tenant_id, row.id, "oauth_token"
            )
            assert credentials["access_token"] == expected_token
            assert credentials["phone_number_id"] == row.external_account_id

        assert await resolve_routing_tenant(session, "whatsapp_coexistence", coexistence_phone) == (
            tenant_id,
            coexistence_row.id,
        )
        assert await resolve_routing_tenant(session, "whatsapp_cloud", cloud_phone) == (
            tenant_id,
            cloud_row.id,
        )
        audit_actions = [
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events WHERE tenant_id = :tid "
                        "AND action = 'channel.whatsapp_connected'"
                    ),
                    {"tid": tenant_id},
                )
            ).all()
        ]
        assert len(audit_actions) == 2


# --------------------------------------------------------------------------------------
# 9. Website LiveChat quick-connect (domain normalization, shared/personal scoping)
# --------------------------------------------------------------------------------------


async def test_web_livechat_connect_normalizes_domain_and_requires_admin(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_id, _ = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id, owner_email = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    harness = build_harness(runtime_session_factory)
    # The site key is the globally unique routing key, so each run claims fresh domains.
    suffix = f"qzled-{uuid4().hex[:8]}.example.com"
    company_domain = f"www.{suffix}"
    personal_domain = f"shop.{suffix}"

    async with harness.client() as client:
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER, owner_email))
        company = await client.post(
            "/api/v1/channels/web-livechat/connect",
            json={"site_domain": f"https://{company_domain}/"},
        )
        assert company.status_code == 200, company.text
        company_body = company.json()
        assert company_body["external_account_id"] == company_domain
        assert company_body["display_name"] == f"LiveChat · {company_domain}"
        assert company_body["visibility_scope"] == "shared"
        assert company_body["owner_user_id"] is None

        # Admins may also hand a widget to a named salesperson; scheme, port, case, path,
        # query, fragment, and trailing slash are all normalized away.
        personal = await client.post(
            "/api/v1/channels/web-livechat/connect",
            json={
                "site_domain": f"HTTP://{personal_domain.upper()}/products?utm_source=qr#top",
                "display_name": "Rep Demo Store",
                "owner_user_id": str(agent_id),
            },
        )
        assert personal.status_code == 200, personal.text
        personal_body = personal.json()
        assert personal_body["external_account_id"] == personal_domain
        assert personal_body["display_name"] == "Rep Demo Store"
        assert personal_body["owner_user_id"] == str(agent_id)
        assert personal_body["visibility_scope"] == "personal"

        invalid = await client.post(
            "/api/v1/channels/web-livechat/connect",
            json={"site_domain": "not a domain/with space"},
        )
        assert invalid.status_code == 422, invalid.text
        assert invalid.json()["code"] == "ERR_INVALID_CHANNEL_CONNECTION"

        # Sales roles cannot register website channels at all.
        harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
        agent_attempt = await client.post(
            "/api/v1/channels/web-livechat/connect",
            json={"site_domain": "https://agent-store.example.com"},
        )
        assert agent_attempt.status_code == 403, agent_attempt.text
        assert agent_attempt.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, channel_type, external_account_id, display_name, config, "
                    "owner_user_id, visibility_scope FROM channel_accounts "
                    "WHERE tenant_id = :tid ORDER BY external_account_id"
                ),
                {"tid": tenant_id},
            )
        ).all()
        assert len(rows) == 2
        personal_row, company_row = rows
        assert personal_row.external_account_id == personal_domain
        assert personal_row.channel_type == "web_livechat"
        assert personal_row.config["site_domain"] == personal_domain
        assert personal_row.owner_user_id == agent_id
        assert personal_row.visibility_scope == "personal"
        assert company_row.external_account_id == company_domain
        assert company_row.config["site_domain"] == company_domain
        assert company_row.owner_user_id is None
        assert company_row.visibility_scope == "shared"
        assert await resolve_routing_tenant(session, "web_livechat", company_domain) == (
            tenant_id,
            company_row.id,
        )
        assert await resolve_routing_tenant(session, "web_livechat", personal_domain) == (
            tenant_id,
            personal_row.id,
        )
        audit_actions = [
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events WHERE tenant_id = :tid "
                        "AND action = 'channel.web_livechat_connected'"
                    ),
                    {"tid": tenant_id},
                )
            ).all()
        ]
        assert len(audit_actions) == 2
