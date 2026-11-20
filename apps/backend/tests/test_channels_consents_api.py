from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.settings_router import list_channel_consents
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginationParams
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.security import hash_password


async def _seed_consent_workspace(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str, str, str, UUID]:
    tenant_id = uuid4()
    customer_id = uuid4()
    other_customer_id = uuid4()
    email = f"owner-{tenant_id.hex[:8]}@example.com"
    password = "ConsentPass2026!"
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Consent', :slug)"),
            {"tid": tenant_id, "slug": f"consent-{tenant_id.hex[:8]}"},
        )
        user_id = await session.scalar(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Consent Owner', :ph) RETURNING id"
            ),
            {"id": uuid4(), "email": email, "ph": hash_password(password)},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'owner')"
            ),
            {"id": uuid4(), "tid": tenant_id, "uid": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) VALUES "
                "(:cid, :tid, 'b2b_contact', 'Hans Weber'), "
                "(:oid, :tid, 'b2c_consumer', 'Elena Vance')"
            ),
            {"cid": customer_id, "oid": other_customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO consent_records (id, tenant_id, customer_id, channel_type, "
                "consent_status, proof_source, recorded_at) VALUES "
                "(:id1, :tid, :cid, 'whatsapp_cloud', 'opted_in', 'web_form_clickwrap', now()), "
                "(:id2, :tid, :oid, 'email', 'opted_out', 'unsubscribe_link', now())"
            ),
            {
                "id1": uuid4(),
                "id2": uuid4(),
                "tid": tenant_id,
                "cid": customer_id,
                "oid": other_customer_id,
            },
        )
        slug = await session.scalar(
            text("SELECT slug FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )
    return tenant_id, str(slug), email, password, UUID(str(user_id))


@pytest.mark.asyncio
async def test_channel_consents_listing_returns_customer_names_without_not_implemented_error(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A correlated customer-name column must compile inside a subquery (SQLAlchemy 2.1).

    `text(...).label()` raised `NotImplementedError: TextClause cannot be used in a
    subquery`; the consents endpoint must stay a 200 with the joined customer names.
    """
    tenant_id, slug, email, password, _owner_user_id = await _seed_consent_workspace(
        runtime_session_factory
    )

    login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": slug},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await async_client.get("/api/v1/channels/consents", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    names_by_channel = {item["channel_type"]: item["customer_name"] for item in body["items"]}
    assert names_by_channel == {"whatsapp_cloud": "Hans Weber", "email": "Elena Vance"}
    assert {item["consent_status"] for item in body["items"]} == {"opted_in", "opted_out"}

    page = await async_client.get("/api/v1/channels/consents", headers=headers, params={"limit": 1})
    assert page.status_code == 200, page.text
    paged = page.json()
    assert paged["total"] == 2
    assert len(paged["items"]) == 1
    assert paged["has_more"] is True

    # Tenant scoping: a foreign tenant's principal never sees this workspace's consents.
    other_tenant_id, other_slug, other_email, other_password, _ = await _seed_consent_workspace(
        runtime_session_factory
    )
    other_login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": other_email, "password": other_password, "tenant_slug": other_slug},
    )
    assert other_login.status_code == 200, other_login.text
    other = await async_client.get(
        "/api/v1/channels/consents",
        headers={"Authorization": f"Bearer {other_login.json()['access_token']}"},
    )
    assert other.status_code == 200, other.text
    seen_tenants = {item["id"] for item in other.json()["items"]}
    assert seen_tenants

    async with tenant_session(runtime_session_factory, other_tenant_id) as session:
        own_rows = (await session.execute(text("SELECT id FROM consent_records"))).scalars().all()
    assert seen_tenants == {str(row) for row in own_rows}
    assert other_tenant_id != tenant_id


@pytest.mark.asyncio
async def test_consents_service_returns_paginated_rows_for_a_direct_session(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Direct service call: the literal correlated column must not raise NotImplementedError."""
    tenant_id, _slug, _email, _password, owner_user_id = await _seed_consent_workspace(
        runtime_session_factory
    )
    principal = CurrentPrincipal(
        user_id=owner_user_id,
        tenant_id=tenant_id,
        role=UserRole.OWNER,
        email="owner@consent.example.com",
    )
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        page = await list_channel_consents(
            session=session,
            principal=principal,
            page=PaginationParams(limit=10, offset=0),
        )
    assert page.total == 2
    assert [item.customer_name for item in page.items]
