from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.security import create_access_token


@pytest.fixture
async def workspace(runtime_session_factory: async_sessionmaker[AsyncSession]):
    tid, other, owner, agent, guest, buyer, foreign, product, variant, warehouse = (
        uuid4() for _ in range(10)
    )
    headers = []
    for tenant, user, role in (
        (tid, owner, UserRole.OWNER),
        (tid, agent, UserRole.AGENT),
        (other, guest, UserRole.OWNER),
    ):
        email = f"{user.hex}@example.com"
        async with tenant_session(runtime_session_factory, tenant) as session:
            p = {"tid": tenant, "uid": user, "email": email, "slug": tenant.hex, "role": role}
            for stmt in (
                "INSERT INTO tenants (id, slug, name) VALUES (:tid,:slug,'Phase 3A') "
                "ON CONFLICT (id) DO NOTHING",
                "INSERT INTO users (id,email,full_name,password_hash) "
                "VALUES (:uid,:email,'Test','x')",
                "INSERT INTO tenant_memberships (tenant_id,user_id,role) VALUES (:tid,:uid,:role)",
            ):
                await session.execute(text(stmt), p)
        token = create_access_token(
            tenant_id=tenant, user_id=user, role=role, email=email, settings=get_settings()
        )
        headers.append({"Authorization": f"Bearer {token}"})

    for tenant, customer, name in (
        (tid, buyer, "Alpha Needle"),
        (other, foreign, "Foreign Needle"),
    ):
        async with tenant_session(runtime_session_factory, tenant) as session:
            await session.execute(
                text("INSERT INTO customers (id,tenant_id,display_name) VALUES (:cid,:tid,:name)"),
                {"cid": customer, "tid": tenant, "name": name},
            )
    async with tenant_session(runtime_session_factory, tid) as session:
        for stmt in (
            "INSERT INTO products (id,tenant_id,spu_code,category) VALUES (:pid,:tid,'P-1','LED')",
            "INSERT INTO product_variants (id,tenant_id,product_id,sku,retail_price_usd,"
            "cost_price_base,weight_grams) VALUES (:vid,:tid,:pid,'SKU-NEEDLE',10,1,100)",
            "INSERT INTO inventory_balances (tenant_id,warehouse_id,variant_id,on_hand_qty,"
            "reserved_qty) VALUES (:tid,:wid,:vid,10,2)",
        ):
            await session.execute(
                text(stmt), {"tid": tid, "pid": product, "vid": variant, "wid": warehouse}
            )
    return tid, owner, buyer, foreign, variant, warehouse, headers


async def test_crm_conversion_followups_and_tasks(async_client: AsyncClient, workspace):
    _, owner, buyer, foreign, _, _, (auth, _, outsider) = workspace
    b2b = "/api/v1/b2b"
    response = await async_client.post(
        f"{b2b}/leads",
        headers=auth,
        json={
            "customer_id": str(foreign),
            "source_channel": "email",
        },
    )
    assert response.status_code == 404
    response = await async_client.post(
        f"{b2b}/leads",
        headers=auth,
        json={
            "customer_id": str(buyer),
            "source_channel": "email",
        },
    )
    assert response.status_code == 201, response.text
    lid = response.json()["id"]
    response = await async_client.patch(
        f"{b2b}/leads/{lid}",
        headers=auth,
        json={
            "status": "qualified",
            "qualification_score": 90,
        },
    )
    assert response.status_code == 200 and response.json()["qualification_score"] == 90
    opp = {
        "lead_id": lid,
        "customer_id": str(buyer),
        "title": "Needle deal",
        "stage": "discovery",
        "owner_user_id": str(owner),
        "expected_amount": "123.45",
    }
    response = await async_client.post(f"{b2b}/opportunities", headers=auth, json=opp)
    assert response.status_code == 201, response.text
    oid = response.json()["id"]
    assert (
        await async_client.post(f"{b2b}/opportunities", headers=auth, json=opp)
    ).status_code == 409
    leads = await async_client.get(f"{b2b}/leads?lead_status=converted", headers=auth)
    assert leads.json()["items"][0]["status"] == "converted"
    response = await async_client.post(
        f"{b2b}/followups",
        headers=auth,
        json={
            "customer_id": str(buyer),
            "opportunity_id": oid,
            "followup_type": "call",
            "summary": "Sent sample information",
        },
    )
    assert response.status_code == 201, response.text
    fid = response.json()["id"]
    response = await async_client.patch(
        f"{b2b}/followups/{fid}", headers=auth, json={"summary": "Sample confirmed"}
    )
    assert response.status_code == 200 and response.json()["author_user_id"] == str(owner)
    assert (await async_client.get(f"{b2b}/followups", headers=outsider)).json()["items"] == []
    response = await async_client.post(
        f"{b2b}/tasks",
        headers=auth,
        json={
            "title": "Send sample",
            "linked_entity_type": "opportunity",
            "linked_entity_id": oid,
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    response = await async_client.patch(
        f"{b2b}/tasks/{task_id}", headers=auth, json={"status": "done"}
    )
    assert response.status_code == 200 and response.json()["status"] == "done"
    assert (
        await async_client.patch(
            f"{b2b}/tasks/{task_id}", headers=outsider, json={"status": "done"}
        )
    ).status_code == 404


async def test_po_concurrent_receipt_and_stocktake_use_one_inventory_ledger(
    async_client: AsyncClient,
    workspace,
    runtime_session_factory: async_sessionmaker[AsyncSession],
):
    tid, _, _, _, variant, warehouse, (auth, agent, outsider) = workspace
    root = "/api/v1/supply-chain"
    res = await async_client.post(
        f"{root}/suppliers", headers=auth, json={"code": "SUP-01", "name": "Component Vendor"}
    )
    assert res.status_code == 201, res.text
    supplier = res.json()["id"]
    payload = {
        "supplier_id": supplier,
        "warehouse_id": str(warehouse),
        "currency": "USD",
        "items": [{"variant_id": str(variant), "qty_ordered": 5, "unit_cost": "1.2345"}],
    }
    assert (
        await async_client.post(f"{root}/purchase-orders", headers=outsider, json=payload)
    ).status_code == 404
    res = await async_client.post(f"{root}/purchase-orders", headers=auth, json=payload)
    assert res.status_code == 201, res.text
    po = res.json()
    assert Decimal(po["total_amount"]) == Decimal("6.17")
    item_id = po["items"][0]["id"]
    url = f"{root}/purchase-orders/{po['id']}/receive"
    payloads = [
        {"receipt_id": str(uuid4()), "items": [{"item_id": item_id, "qty": 3}]} for _ in range(2)
    ]
    responses = await asyncio.gather(
        *(async_client.post(url, headers=auth, json=p) for p in payloads),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    for response, original in zip(responses, payloads, strict=True):
        if response.status_code == 200:
            replay = await async_client.post(url, headers=auth, json=original)
            assert replay.status_code == 200 and replay.json()["items"][0]["qty_received"] == 3
    assert (
        await async_client.get(f"{root}/purchase-orders/{po['id']}", headers=outsider)
    ).status_code == 404
    assert (await async_client.get(f"{root}/purchase-orders", headers=agent)).status_code == 403

    wms_url = "/api/v1/wms/movements"
    m = {
        "movement_type": "stocktake_adjust",
        "warehouse_id": str(warehouse),
        "variant_id": str(variant),
        "qty_delta": -2,
        "request_id": str(uuid4()),
    }
    assert (await async_client.post(wms_url, headers=agent, json=m)).status_code == 403
    for kind in ("inbound_po", "outbound_order", "transfer"):
        assert (
            await async_client.post(wms_url, headers=auth, json={**m, "movement_type": kind})
        ).status_code == 422
    assert (
        await async_client.post(wms_url, headers=auth, json={**m, "qty_delta": -12})
    ).status_code == 409
    first = await async_client.post(wms_url, headers=auth, json=m)
    assert first.status_code == 201, first.text
    replay = await async_client.post(wms_url, headers=auth, json=m)
    assert replay.json()["id"] == first.json()["id"]
    stock = (await async_client.get("/api/v1/wms/stock", headers=agent)).json()["items"]
    assert stock[0]["available_qty"] == 9
    assert (await async_client.get(wms_url, headers=outsider)).json()["items"] == []
    async with tenant_session(runtime_session_factory, tid) as session:
        assert await session.scalar(text("SELECT on_hand_qty FROM inventory_balances")) == 11
        deltas = await session.scalars(text("SELECT qty_delta_on_hand FROM inventory_ledger"))
        assert sorted(deltas) == [-2, 3]
        count = await session.scalar(text("SELECT count(*) FROM wms_movements"))
        assert count == 2
        ref = await session.scalar(
            text("SELECT reference_id FROM wms_movements WHERE movement_type = 'inbound_po'")
        )
        assert ref == UUID(po["id"])


async def test_dashboard_and_search_scope_financial_data(async_client: AsyncClient, workspace):
    _, _, _, _, _, _, (auth, agent, outsider) = workspace
    dash = "/api/v1/analytics/dashboard"
    owner = await async_client.get(dash, headers=auth)
    assert owner.status_code == 200, owner.text
    assert owner.json()["financials"] is not None
    safe = await async_client.get(dash, headers=agent)
    assert safe.status_code == 200 and safe.json().get("financials") is None
    overview = await async_client.get("/api/v1/analytics/overview", headers=agent)
    assert overview.status_code == 200 and overview.json().get("sku_margins") is None
    result = await async_client.get("/api/v1/search?q=needle", headers=agent)
    assert result.status_code == 200, result.text
    assert {(r["kind"], r["label"]) for r in result.json()["items"]} == {
        ("customer", "Alpha Needle"),
        ("sku", "SKU-NEEDLE"),
    }
    foreign_results = await async_client.get("/api/v1/search?q=needle", headers=outsider)
    assert [r["label"] for r in foreign_results.json()["items"]] == ["Foreign Needle"]
    literal = await async_client.get("/api/v1/search", params={"q": "%_"}, headers=auth)
    assert literal.json()["items"] == []


async def test_phase3a_demo_seed_is_idempotent(runtime_session_factory, workspace):
    """The 3A demo rows must survive a re-run without duplicating purchase orders."""
    import importlib.util
    import sys

    tid, owner, buyer, _, variant, warehouse, _ = workspace
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed_hybrid_factory.py"
    spec = importlib.util.spec_from_file_location("seed_phase3a", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    kwargs = {
        "tenant_id": tid,
        "warehouse_id": warehouse,
        "variant_id": variant,
        "buyer_id": buyer,
        "owner_id": owner,
    }
    for _ in range(2):
        async with tenant_session(runtime_session_factory, tid) as session:
            await module._seed_phase3a_demo(session, **kwargs)
    async with tenant_session(runtime_session_factory, tid) as session:
        counts = (
            await session.execute(
                text("""
            SELECT (SELECT count(*) FROM suppliers), (SELECT count(*) FROM purchase_orders),
                   (SELECT count(*) FROM purchase_order_items),
                   (SELECT count(*) FROM wms_movements), (SELECT count(*) FROM task_reminders)
        """)
            )
        ).one()
        assert tuple(counts) == (1, 2, 2, 1, 2)
        assert (
            await session.scalar(
                text("""
            SELECT count(*) FROM purchase_orders WHERE status = 'received'
        """)
            )
            == 1
        )
