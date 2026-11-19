from __future__ import annotations

import json
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

from erp_core.shared.db import tenant_session


async def test_warehouses_are_tenant_scoped_and_do_not_guess_a_default(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token_a, _, tenant_a = await _register_owner(async_client)
    token_b, _, tenant_b = await _register_owner(async_client)
    stocked, configured, foreign = uuid4(), uuid4(), uuid4()
    for tenant, stocked_id, config_ids in (
        (UUID(tenant_a), stocked, [str(configured), str(configured), "not-a-uuid"]),
        (UUID(tenant_b), foreign, []),
    ):
        async with tenant_session(runtime_session_factory, tenant) as session:
            args = {"tid": tenant, "pid": uuid4(), "vid": uuid4(), "wid": stocked_id}
            await session.execute(
                text("""
                INSERT INTO products (id, tenant_id, spu_code, category)
                VALUES (:pid, :tid, 'SPU-LED', 'LED')
            """),
                args,
            )
            await session.execute(
                text("""
                INSERT INTO product_variants (
                    id, tenant_id, product_id, sku, cost_price_base, retail_price_usd,
                    weight_grams, lead_time_days
                ) VALUES (:vid, :tid, :pid, 'SKU-LED', 40, 15, 200, 3)
            """),
                args,
            )
            await session.execute(
                text("""
                INSERT INTO inventory_balances
                    (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                VALUES (:tid, :wid, :vid, 8, 0)
            """),
                args,
            )
            for config in config_ids:
                await session.execute(
                    text("""
                    INSERT INTO channel_accounts
                        (tenant_id, channel_type, external_account_id, display_name, config)
                    VALUES (:tid, 'shopify', :external, 'Store', CAST(:config AS jsonb))
                """),
                    {
                        "tid": tenant,
                        "external": str(uuid4()),
                        "config": json.dumps({"warehouse_id": config}),
                    },
                )
    url = "/api/v1/orders/warehouses"
    auth_a = {"Authorization": f"Bearer {token_a}"}
    response = await async_client.get(url, headers=auth_a)
    assert response.status_code == 200, response.text
    assert response.json()["default_warehouse_id"] == str(configured)
    assert {row["id"]: row["is_default"] for row in response.json()["items"]} == {
        str(stocked): False,
        str(configured): True,
    }
    other = await async_client.get(url, headers={"Authorization": f"Bearer {token_b}"})
    assert other.status_code == 200, other.text
    assert other.json() == {
        "default_warehouse_id": None,
        "items": [{"id": str(foreign), "is_default": False}],
    }
    async with tenant_session(runtime_session_factory, UUID(tenant_a)) as session:
        await session.execute(
            text("""
            INSERT INTO channel_accounts
                (tenant_id, channel_type, external_account_id, display_name, config)
            VALUES (:tid, 'shopify', :external, 'Another store', CAST(:config AS jsonb))
        """),
            {
                "tid": UUID(tenant_a),
                "external": str(uuid4()),
                "config": json.dumps({"warehouse_id": str(stocked)}),
            },
        )
    conflicting = await async_client.get(url, headers=auth_a)
    assert conflicting.status_code == 200 and conflicting.json()["default_warehouse_id"] is None
    assert all(not item["is_default"] for item in conflicting.json()["items"])
