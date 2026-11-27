from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from httpx import AsyncClient

from erp_core.shared.config import get_settings
from erp_core.shared.enums import UserRole


async def _register_owner(async_client: AsyncClient) -> tuple[str, str, str]:
    suffix = uuid4().hex[:8]
    email = f"owner-{suffix}@factory.example.com"
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "StrongPassword2026!",
            "full_name": "Jerry Lin",
            "tenant_name": f"Factory {suffix}",
            "tenant_slug": f"factory-{suffix}",
            "base_currency": "USD",
            "timezone": "Asia/Shanghai",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["access_token"], data["refresh_token"], data["principal"]["tenant_id"]


async def test_register_login_refresh_and_invalid_jwt_rejected(
    async_client: AsyncClient,
) -> None:
    access_tok, refresh_tok, tenant_id = await _register_owner(async_client)

    me_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == "owner"
    assert me_resp.json()["tenant_id"] == tenant_id
    assert me_resp.json()["can_view_cost"] is True

    # Refresh token flow returns fresh pair
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert ref_resp.status_code == 200
    assert ref_resp.json()["access_token"]

    # Using refresh token as Bearer access token MUST be rejected
    wrong_type_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refresh_tok}"},
    )
    assert wrong_type_resp.status_code == 401
    assert wrong_type_resp.json()["code"] == "ERR_INVALID_TOKEN"

    # Expired JWT MUST be rejected with 401
    settings = get_settings()
    expired_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tid": tenant_id,
            "role": UserRole.OWNER.value,
            "email": "expired@example.com",
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    exp_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert exp_resp.status_code == 401
    assert exp_resp.json()["code"] == "ERR_INVALID_TOKEN"

    # Tampered signature MUST be rejected with 401
    bad_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert bad_resp.status_code == 401


async def test_five_roles_rbac_boundaries_and_agent_cost_masking(
    async_client: AsyncClient,
) -> None:
    owner_tok, _, _ = await _register_owner(async_client)
    role_tokens: dict[str, str] = {"owner": owner_tok}

    for role in ("admin", "manager", "agent", "finance"):
        email = f"{role}-{uuid4().hex[:6]}@factory.example.com"
        password = "RolePassword2026!"
        invite_resp = await async_client.post(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {owner_tok}"},
            json={
                "email": email,
                "full_name": f"User {role}",
                "password": password,
                "role": role,
            },
        )
        assert invite_resp.status_code == 201, invite_resp.text

        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200, login_resp.text
        role_tokens[role] = login_resp.json()["access_token"]

    # 1. Owner-only settings update
    owner_settings = await async_client.patch(
        "/api/v1/tenants/settings",
        headers={"Authorization": f"Bearer {role_tokens['owner']}"},
        json={"base_currency": "EUR"},
    )
    assert owner_settings.status_code == 200

    for denied_role in ("admin", "manager", "agent", "finance"):
        denied = await async_client.patch(
            "/api/v1/tenants/settings",
            headers={"Authorization": f"Bearer {role_tokens[denied_role]}"},
            json={"base_currency": "GBP"},
        )
        assert denied.status_code == 403, (
            f"Expected 403 for {denied_role}, got {denied.status_code}"
        )
        assert denied.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    # 2. Membership management allowed for owner & admin; denied for manager, agent, finance
    for allowed in ("owner", "admin"):
        allowed_resp = await async_client.get(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {role_tokens[allowed]}"},
        )
        assert allowed_resp.status_code == 200
        assert allowed_resp.json()["total"] == 5

    for denied_role in ("manager", "agent", "finance"):
        denied_list = await async_client.get(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {role_tokens[denied_role]}"},
        )
        assert denied_list.status_code == 403

    # 3. Cost & net profit visibility: visible to owner/admin/manager/finance; masked for agent
    for privileged in ("owner", "admin", "manager", "finance"):
        resp = await async_client.get(
            "/api/v1/tenants/cost-visibility-sample",
            headers={"Authorization": f"Bearer {role_tokens[privileged]}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cost_masked"] is False
        assert body["cost_price_base"] == "18.50"
        assert body["net_profit_base"] == "11.20"

    agent_resp = await async_client.get(
        "/api/v1/tenants/cost-visibility-sample",
        headers={"Authorization": f"Bearer {role_tokens['agent']}"},
    )
    assert agent_resp.status_code == 200
    agent_body = agent_resp.json()
    assert agent_body["cost_masked"] is True
    assert agent_body["cost_price_base"] is None
    assert agent_body["cogs_base"] is None
    assert agent_body["net_profit_base"] is None
    assert agent_body["net_margin_pct"] is None
    assert agent_body["unit_sale_price"] == "34.90"


async def test_logout_revokes_access_and_refresh_tokens_and_records_audit(
    async_client: AsyncClient,
) -> None:
    suffix = uuid4().hex[:8]
    email = f"logout-{suffix}@factory.example.com"
    password = "StrongPassword2026!"
    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": "Logout Tester",
            "tenant_name": f"Factory {suffix}",
            "tenant_slug": f"factory-{suffix}",
            "base_currency": "USD",
            "timezone": "Asia/Shanghai",
        },
    )
    assert reg_resp.status_code == 201, reg_resp.text
    reg_data = reg_resp.json()
    access_tok = reg_data["access_token"]
    refresh_tok = reg_data["refresh_token"]

    logout_resp = await async_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_tok}"},
        json={"refresh_token": refresh_tok},
    )
    assert logout_resp.status_code == 200, logout_resp.text
    assert logout_resp.json() == {"ok": True}

    me_after = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert me_after.status_code == 401
    assert me_after.json()["code"] == "ERR_INVALID_TOKEN"

    refresh_after = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert refresh_after.status_code == 401
    assert refresh_after.json()["code"] == "ERR_INVALID_TOKEN"

    login_again = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_again.status_code == 200, login_again.text
    fresh_access = login_again.json()["access_token"]

    audit_resp = await async_client.get(
        "/api/v1/tenancy/audit-events?action=auth.logout",
        headers={"Authorization": f"Bearer {fresh_access}"},
    )
    assert audit_resp.status_code == 200, audit_resp.text
    items = audit_resp.json()["items"]
    assert len(items) >= 1
    assert items[0]["after_state"]["revoked_access_token"] is True
    assert items[0]["after_state"]["revoked_refresh_token"] is True

    # Logout without a JSON body also succeeds cleanly and revokes the access token.
    logout_empty = await async_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {fresh_access}"},
    )
    assert logout_empty.status_code == 200, logout_empty.text
    assert logout_empty.json() == {"ok": True}
    me_after_empty = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {fresh_access}"},
    )
    assert me_after_empty.status_code == 401
