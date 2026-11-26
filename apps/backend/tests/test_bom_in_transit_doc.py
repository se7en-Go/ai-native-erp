"""Track 7 backend suite: 1-level light BOM, in-transit headhaul transfers, and
days-of-cover (DOC) weighted replenishment.

Everything runs against the real PG17 test database as the non-superuser
`app_runtime` role through `tenant_session`, so the assertions also prove the
`FORCE ROW LEVEL SECURITY` policies on the three new tables (0026) rather than a
mocked repository layer.

Decimal expectations are written as the exact strings the API returns: money and
quantities are never re-derived in a float here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register the domain model modules so SQLAlchemy can resolve the cross-module
# string foreign keys (mirrors the api_main composition root).
import erp_core.catalog.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.supply_chain.bom_doc_models  # noqa: F401
import erp_core.supply_chain.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.supply_chain import bom_doc_service
from erp_core.supply_chain.bom_doc_router import router as bom_doc_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password

RuntimeFactory = async_sessionmaker[AsyncSession]

API = "/api/v1/supply-chain"


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
    app.include_router(bom_doc_router)
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


async def create_workspace(factory: RuntimeFactory, label: str = "Track7") -> UUID:
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
                "email": f"{role.value}-{user_id.hex[:12]}@track7.test",
                "name": f"{role.value.title()} Person",
                "ph": hash_password("Track7Pass2026!"),
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


async def seed_warehouse(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    code: str,
    name: str,
    country_code: str = "CN",
) -> UUID:
    warehouse_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city) "
                "VALUES (:id, :tid, :code, :name, :country, :city)"
            ),
            {
                "id": warehouse_id,
                "tid": tenant_id,
                "code": code,
                "name": name,
                "country": country_code,
                "city": "Shenzhen" if country_code == "CN" else "Los Angeles",
            },
        )
    return warehouse_id


async def seed_variant(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    sku: str,
    cost_price_base: str = "10.0000",
    base_currency: str = "CNY",
    product_name: str | None = None,
) -> UUID:
    product_id = uuid4()
    variant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category, i18n_specs) "
                "VALUES (:id, :tid, :spu, 'led_lighting', CAST(:specs AS jsonb))"
            ),
            {
                "id": product_id,
                "tid": tenant_id,
                "spu": f"SPU-{sku}",
                "specs": f'{{"en": {{"title": "{product_name or sku}"}}}}',
            },
        )
        await session.execute(
            text(
                "INSERT INTO product_variants "
                "(id, tenant_id, product_id, sku, cost_price_base, base_currency, "
                " retail_price_usd, weight_grams) "
                "VALUES (:id, :tid, :pid, :sku, :cost, :ccy, 29.90, 300)"
            ),
            {
                "id": variant_id,
                "tid": tenant_id,
                "pid": product_id,
                "sku": sku,
                "cost": Decimal(cost_price_base),
                "ccy": base_currency,
            },
        )
    return variant_id


async def seed_stock(
    factory: RuntimeFactory,
    tenant_id: UUID,
    warehouse_id: UUID,
    variant_id: UUID,
    qty: int,
) -> None:
    """Seed real on-hand stock through the inventory ledger (not a raw balance row)."""
    async with tenant_session(factory, tenant_id) as session:
        await InventoryLedgerService().inbound_stock(
            session,
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            qty=qty,
            reference_id=uuid4(),
            idempotency_key=f"seed-inbound-{tenant_id}-{warehouse_id}-{variant_id}",
        )


async def seed_customer(factory: RuntimeFactory, tenant_id: UUID) -> UUID:
    customer_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', 'Doc Buyer')"
            ),
            {"id": customer_id, "tid": tenant_id},
        )
    return customer_id


async def seed_order(
    factory: RuntimeFactory,
    tenant_id: UUID,
    variant_id: UUID,
    *,
    quantity: int,
    days_ago: int,
    fulfillment_status: str = "delivered",
) -> UUID:
    customer_id = await seed_customer(factory, tenant_id)
    order_id = uuid4()
    created_at = datetime.now(UTC) - timedelta(days=days_ago)
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency, "
                " fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, "
                " payment_status, created_at, updated_at) "
                "VALUES (:id, :tid, :no, 'b2c_retail', :cid, 'USD', 7.100000, :amount, "
                " :amount, :status, 'paid', :created, :created)"
            ),
            {
                "id": order_id,
                "tid": tenant_id,
                "no": f"ORD-{order_id.hex[:12]}",
                "cid": customer_id,
                "amount": Decimal(quantity) * Decimal("10.00"),
                "status": fulfillment_status,
                "created": created_at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO order_items (id, tenant_id, order_id, variant_id, sku_snapshot, "
                " quantity, unit_price, unit_cost_base_snapshot, line_total) "
                "VALUES (:id, :tid, :oid, :vid, 'SKU-SNAPSHOT', :qty, 10.0000, 6.0000, :total)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "oid": order_id,
                "vid": variant_id,
                "qty": quantity,
                "total": Decimal(quantity) * Decimal("10.00"),
            },
        )
    return order_id


async def on_hand(
    factory: RuntimeFactory, tenant_id: UUID, warehouse_id: UUID, variant_id: UUID
) -> int:
    async with tenant_session(factory, tenant_id) as session:
        balance = await session.scalar(
            text(
                "SELECT on_hand_qty FROM inventory_balances "
                "WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
            ),
            {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
        )
        return int(balance or 0)


async def ledger_balance(
    factory: RuntimeFactory, tenant_id: UUID, warehouse_id: UUID, variant_id: UUID
) -> int:
    async with tenant_session(factory, tenant_id) as session:
        delta = await session.scalar(
            text(
                "SELECT COALESCE(SUM(qty_delta_on_hand), 0) FROM inventory_ledger "
                "WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
            ),
            {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
        )
        return int(delta or 0)


# ----------------------------------------------------------------------------
# 1. Migration: FORCE RLS, policies, grants, constraints
# ----------------------------------------------------------------------------


async def test_migration_0026_forces_rls_and_grants_app_runtime(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Migration")
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        for table in ("bom_components", "stock_transfers", "stock_transfer_lines"):
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


async def test_migration_0026_check_constraints_are_enforced_by_postgres(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Checks")
    kit = await seed_variant(runtime_session_factory, tenant_id, sku="KIT-CHECK")
    component = await seed_variant(runtime_session_factory, tenant_id, sku="PART-CHECK")
    source = await seed_warehouse(runtime_session_factory, tenant_id, code="CN-SZ", name="SZ")
    destination = await seed_warehouse(
        runtime_session_factory, tenant_id, code="US-FBA", name="US FBA", country_code="US"
    )

    # A kit can never be its own component.
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO bom_components (tenant_id, parent_variant_id, "
                    " component_variant_id, qty_per_unit) VALUES (:tid, :vid, :vid, 1)"
                ),
                {"tid": tenant_id, "vid": kit},
            )

    # qty_per_unit > 0 and waste_rate_pct within 0..100.
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO bom_components (tenant_id, parent_variant_id, "
                    " component_variant_id, qty_per_unit) VALUES (:tid, :kit, :part, 0)"
                ),
                {"tid": tenant_id, "kit": kit, "part": component},
            )
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO bom_components (tenant_id, parent_variant_id, "
                    " component_variant_id, qty_per_unit, waste_rate_pct) "
                    "VALUES (:tid, :kit, :part, 1, 120)"
                ),
                {"tid": tenant_id, "kit": kit, "part": component},
            )

    # Transfer status / shipping method / ETA / distinct warehouses are CHECKed.
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO stock_transfers (tenant_id, transfer_no, source_warehouse_id, "
                    " destination_warehouse_id, status) "
                    "VALUES (:tid, 'BAD-STATUS', :src, :dst, 'lost')"
                ),
                {"tid": tenant_id, "src": source, "dst": destination},
            )
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO stock_transfers (tenant_id, transfer_no, source_warehouse_id, "
                    " destination_warehouse_id, shipping_method) "
                    "VALUES (:tid, 'BAD-METHOD', :src, :dst, 'teleport')"
                ),
                {"tid": tenant_id, "src": source, "dst": destination},
            )
    with pytest.raises(IntegrityError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO stock_transfers (tenant_id, transfer_no, source_warehouse_id, "
                    " destination_warehouse_id) VALUES (:tid, 'SAME-WH', :src, :src)"
                ),
                {"tid": tenant_id, "src": source},
            )


async def test_new_tables_isolate_tenants_under_force_rls(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_a = await create_workspace(runtime_session_factory, "RlsA")
    tenant_b = await create_workspace(runtime_session_factory, "RlsB")
    owner_a = await add_member(runtime_session_factory, tenant_a, UserRole.OWNER)
    kit_a = await seed_variant(runtime_session_factory, tenant_a, sku="KIT-A")
    part_a = await seed_variant(runtime_session_factory, tenant_a, sku="PART-A")
    source_a = await seed_warehouse(runtime_session_factory, tenant_a, code="CN-A", name="A")
    destination_a = await seed_warehouse(
        runtime_session_factory, tenant_a, code="US-A", name="A FBA", country_code="US"
    )

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_a, owner_a, UserRole.OWNER))
    async with harness.client() as client:
        created = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit_a),
                "component_variant_id": str(part_a),
                "qty_per_unit": "2.0000",
            },
        )
        assert created.status_code == 201, created.text
        transfer = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source_a),
                "destination_warehouse_id": str(destination_a),
                "lines": [{"variant_id": str(part_a), "qty": 7}],
            },
        )
        assert transfer.status_code == 201, transfer.text

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        for table in ("bom_components", "stock_transfers", "stock_transfer_lines"):
            leaked = await session.scalar(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_a},
            )
            assert leaked == 0, table
        # RLS is fail-closed: with no tenant setting the table reads as empty.
        await session.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
        assert await session.scalar(text("SELECT COUNT(*) FROM bom_components")) == 0


# ----------------------------------------------------------------------------
# 2. 1-level BOM: validation, rollup, assembly
# ----------------------------------------------------------------------------


async def test_bom_rejects_self_reference_and_second_level(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "BomGuard")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    kit = await seed_variant(runtime_session_factory, tenant_id, sku="KIT-5IN1")
    strip = await seed_variant(runtime_session_factory, tenant_id, sku="LED-COB-5M")
    driver = await seed_variant(runtime_session_factory, tenant_id, sku="DRIVER-60W")

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        ok = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(strip),
                "qty_per_unit": "2.0000",
                "waste_rate_pct": "5.00",
                "notes": "2 strips per kit",
            },
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["effective_qty_per_unit"] == "2.1000"

        self_ref = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(kit),
                "qty_per_unit": "1.0000",
            },
        )
        assert self_ref.status_code == 422
        assert self_ref.json()["code"] == "ERR_BOM_SELF_REFERENCE"

        # The kit may not become a component of another kit (would be level 2).
        nested_kit = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(driver),
                "component_variant_id": str(kit),
                "qty_per_unit": "1.0000",
            },
        )
        assert nested_kit.status_code == 422
        assert nested_kit.json()["code"] == "ERR_BOM_MULTI_LEVEL"

        # A component may not itself be a kit (the other nesting direction).
        ok_driver = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(driver),
                "qty_per_unit": "1.0000",
            },
        )
        assert ok_driver.status_code == 201, ok_driver.text
        nested_component = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(driver),
                "component_variant_id": str(strip),
                "qty_per_unit": "1.0000",
            },
        )
        assert nested_component.status_code == 422
        assert nested_component.json()["code"] == "ERR_BOM_MULTI_LEVEL"

        upserted = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(strip),
                "qty_per_unit": "2.5000",
                "waste_rate_pct": "0.00",
            },
        )
        assert upserted.status_code == 201, upserted.text
        assert upserted.json()["qty_per_unit"] == "2.5000"

        listed = await client.get(f"{API}/bom")
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 1
        kit_row = body["items"][0]
        assert kit_row["parent_sku"] == "KIT-5IN1"
        assert kit_row["component_count"] == 2
        assert {item["component_sku"] for item in kit_row["components"]} == {
            "LED-COB-5M",
            "DRIVER-60W",
        }

        deleted = await client.delete(
            f"{API}/bom/components/{upserted.json()['id']}"
        )
        assert deleted.status_code == 204
        after = (await client.get(f"{API}/bom")).json()
        assert after["items"][0]["component_count"] == 1


async def test_bom_cost_rollup_uses_exact_decimal(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Rollup")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    kit = await seed_variant(runtime_session_factory, tenant_id, sku="KIT-ROLLUP")
    strip = await seed_variant(
        runtime_session_factory, tenant_id, sku="LED-COB-5M", cost_price_base="12.3456"
    )
    driver = await seed_variant(
        runtime_session_factory, tenant_id, sku="DRIVER-60W", cost_price_base="3.0000"
    )

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        for component, qty, waste in ((strip, "2.5000", "10.00"), (driver, "1.0000", "0.00")):
            created = await client.post(
                f"{API}/bom/components",
                json={
                    "parent_variant_id": str(kit),
                    "component_variant_id": str(component),
                    "qty_per_unit": qty,
                    "waste_rate_pct": waste,
                },
            )
            assert created.status_code == 201, created.text

        rollup = await client.get(f"{API}/bom/{kit}/cost-rollup")
        assert rollup.status_code == 200
        body = rollup.json()
        assert body["base_currency"] == "CNY"
        assert body["component_count"] == 2
        # 12.3456 * 2.5 * 1.10 = 33.9504 ; 3.0000 * 1 = 3.0000
        by_sku = {line["component_sku"]: line for line in body["lines"]}
        assert by_sku["LED-COB-5M"]["effective_qty_per_unit"] == "2.7500"
        assert by_sku["LED-COB-5M"]["line_unit_cost_base"] == "33.9504"
        assert by_sku["DRIVER-60W"]["line_unit_cost_base"] == "3.0000"
        assert body["unit_cost_base"] == "36.9504"


async def test_assemble_kit_deducts_components_and_is_idempotent(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Assemble")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    warehouse = await seed_warehouse(runtime_session_factory, tenant_id, code="CN-SZ", name="SZ")
    kit = await seed_variant(runtime_session_factory, tenant_id, sku="KIT-ASM")
    strip = await seed_variant(runtime_session_factory, tenant_id, sku="LED-COB-5M")
    driver = await seed_variant(runtime_session_factory, tenant_id, sku="DRIVER-60W")
    await seed_stock(runtime_session_factory, tenant_id, warehouse, strip, 10)
    await seed_stock(runtime_session_factory, tenant_id, warehouse, driver, 5)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        for component, qty, waste in ((strip, "2.0000", "0.00"), (driver, "1.0000", "50.00")):
            created = await client.post(
                f"{API}/bom/components",
                json={
                    "parent_variant_id": str(kit),
                    "component_variant_id": str(component),
                    "qty_per_unit": qty,
                    "waste_rate_pct": waste,
                },
            )
            assert created.status_code == 201, created.text

        request_id = uuid4()
        payload = {
            "warehouse_id": str(warehouse),
            "qty_kits": 3,
            "request_id": str(request_id),
            "notes": "Wave 1 kit build",
        }
        assembled = await client.post(f"{API}/bom/{kit}/assemble", json=payload)
        assert assembled.status_code == 201, assembled.text
        body = assembled.json()
        assert body["replayed"] is False
        assert body["parent_on_hand_qty"] == 3
        # 2.0 * 3 = 6 strips; 1.5 * 3 = 4.5 -> 5 drivers (rounded up, never short).
        consumed = {row["component_sku"]: row["qty_consumed"] for row in body["consumed"]}
        assert consumed == {"LED-COB-5M": 6, "DRIVER-60W": 5}

        assert await on_hand(runtime_session_factory, tenant_id, warehouse, strip) == 4
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, driver) == 0
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, kit) == 3
        # The ledger stays a single source of truth for every warehouse.
        for variant in (strip, driver, kit):
            assert await ledger_balance(
                runtime_session_factory, tenant_id, warehouse, variant
            ) == await on_hand(runtime_session_factory, tenant_id, warehouse, variant)

        replay = await client.post(f"{API}/bom/{kit}/assemble", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["replayed"] is True
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, strip) == 4
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, kit) == 3

        # A different request_id with insufficient child stock must roll back whole.
        starved = await client.post(
            f"{API}/bom/{kit}/assemble",
            json={
                "warehouse_id": str(warehouse),
                "qty_kits": 100,
                "request_id": str(uuid4()),
            },
        )
        assert starved.status_code == 409
        assert starved.json()["code"] == "ERR_INSUFFICIENT_STOCK"
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, kit) == 3
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, strip) == 4

        # Reusing a consumed request_id for a different payload is refused, not replayed.
        reused = await client.post(
            f"{API}/bom/{kit}/assemble",
            json={**payload, "qty_kits": 1},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "ERR_STATE_CONFLICT"
        assert await on_hand(runtime_session_factory, tenant_id, warehouse, kit) == 3


# ----------------------------------------------------------------------------
# 3. In-transit headhaul stock transfers
# ----------------------------------------------------------------------------


async def test_transfer_dispatch_and_receive_move_stock_through_transit(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Transit")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    source = await seed_warehouse(
        runtime_session_factory, tenant_id, code="CN-SZ", name="SZ Factory"
    )
    destination = await seed_warehouse(
        runtime_session_factory, tenant_id, code="US-FBA", name="US FBA", country_code="US"
    )
    strip = await seed_variant(runtime_session_factory, tenant_id, sku="LED-COB-5M")
    driver = await seed_variant(runtime_session_factory, tenant_id, sku="DRIVER-60W")
    await seed_stock(runtime_session_factory, tenant_id, source, strip, 100)
    await seed_stock(runtime_session_factory, tenant_id, source, driver, 60)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        created = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source),
                "destination_warehouse_id": str(destination),
                "shipping_method": "sea_freight",
                "tracking_no": "MATS-7788",
                "eta_days": 32,
                "lines": [
                    {"variant_id": str(strip), "qty": 40, "unit_freight_cost_base": "1.2500"},
                    {"variant_id": str(driver), "qty": 20, "unit_freight_cost_base": "0.7500"},
                ],
            },
        )
        assert created.status_code == 201, created.text
        transfer = created.json()
        transfer_id = transfer["id"]
        assert transfer["status"] == "draft"
        assert transfer["transfer_no"].startswith("TRF-")
        assert transfer["source_warehouse_name"] == "SZ Factory"
        assert transfer["destination_warehouse_name"] == "US FBA"
        assert transfer["total_qty"] == 60
        # 40 * 1.25 + 20 * 0.75 = 65.0000
        assert transfer["total_freight_cost_base"] == "65.0000"

        same_warehouse = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source),
                "destination_warehouse_id": str(source),
                "lines": [{"variant_id": str(strip), "qty": 1}],
            },
        )
        assert same_warehouse.status_code == 422
        assert same_warehouse.json()["code"] == "ERR_TRANSFER_SAME_WAREHOUSE"

        duplicate = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source),
                "destination_warehouse_id": str(destination),
                "lines": [
                    {"variant_id": str(strip), "qty": 1},
                    {"variant_id": str(strip), "qty": 2},
                ],
            },
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["code"] == "ERR_TRANSFER_DUPLICATE_SKU"

        dispatched = await client.post(f"{API}/transfers/{transfer_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["status"] == "in_transit"
        assert dispatched.json()["dispatched_at"] is not None
        assert await on_hand(runtime_session_factory, tenant_id, source, strip) == 60
        assert await on_hand(runtime_session_factory, tenant_id, destination, strip) == 0

        # Dispatch is idempotent: a retry must not deduct the source a second time.
        repeated = await client.post(f"{API}/transfers/{transfer_id}/dispatch")
        assert repeated.status_code == 200
        assert await on_hand(runtime_session_factory, tenant_id, source, strip) == 60

        # The in-transit leg is visible to the DOC model while the load is at sea.
        doc = await client.get(f"{API}/doc-replenishment?target_doc_days=30")
        assert doc.status_code == 200, doc.text
        transit = {row["sku"]: row["in_transit_qty"] for row in doc.json()["items"]}
        assert transit["LED-COB-5M"] == 40
        assert transit["DRIVER-60W"] == 20

        received = await client.post(f"{API}/transfers/{transfer_id}/receive")
        assert received.status_code == 200, received.text
        assert received.json()["status"] == "received"
        assert received.json()["received_at"] is not None
        assert {line["received_qty"] for line in received.json()["lines"]} == {40, 20}
        assert await on_hand(runtime_session_factory, tenant_id, destination, strip) == 40
        assert await on_hand(runtime_session_factory, tenant_id, destination, driver) == 20
        assert await on_hand(runtime_session_factory, tenant_id, source, strip) == 60

        again = await client.post(f"{API}/transfers/{transfer_id}/receive")
        assert again.status_code == 200
        assert await on_hand(runtime_session_factory, tenant_id, destination, strip) == 40

        # A received load can neither be re-dispatched nor cancelled.
        re_dispatch = await client.post(f"{API}/transfers/{transfer_id}/dispatch")
        assert re_dispatch.status_code == 409
        assert re_dispatch.json()["code"] == "ERR_STATE_CONFLICT"
        cancelled_received = await client.post(f"{API}/transfers/{transfer_id}/cancel")
        assert cancelled_received.status_code == 409

        # Draft transfers can be cancelled; cancelled ones cannot be dispatched.
        draft = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source),
                "destination_warehouse_id": str(destination),
                "lines": [{"variant_id": str(strip), "qty": 5}],
            },
        )
        assert draft.status_code == 201, draft.text
        cancelled = await client.post(f"{API}/transfers/{draft.json()['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert await on_hand(runtime_session_factory, tenant_id, source, strip) == 60
        refused = await client.post(f"{API}/transfers/{draft.json()['id']}/dispatch")
        assert refused.status_code == 409

        listed = await client.get(f"{API}/transfers")
        assert listed.status_code == 200
        assert listed.json()["total"] == 2


async def test_transfer_dispatch_rejects_insufficient_source_stock(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "TransitShort")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    source = await seed_warehouse(runtime_session_factory, tenant_id, code="CN-SZ", name="SZ")
    destination = await seed_warehouse(
        runtime_session_factory, tenant_id, code="US-FBA", name="US FBA", country_code="US"
    )
    strip = await seed_variant(runtime_session_factory, tenant_id, sku="LED-COB-5M")
    await seed_stock(runtime_session_factory, tenant_id, source, strip, 4)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        created = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(source),
                "destination_warehouse_id": str(destination),
                "lines": [{"variant_id": str(strip), "qty": 10}],
            },
        )
        assert created.status_code == 201, created.text
        dispatched = await client.post(f"{API}/transfers/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 409
        assert dispatched.json()["code"] == "ERR_INSUFFICIENT_STOCK"
        # Nothing left the dock and the transfer is still a draft.
        assert await on_hand(runtime_session_factory, tenant_id, source, strip) == 4
        listed = await client.get(f"{API}/transfers")
        assert listed.json()["items"][0]["status"] == "draft"


# ----------------------------------------------------------------------------
# 4. Days-of-cover weighted replenishment
# ----------------------------------------------------------------------------


async def test_doc_replenishment_blends_windows_and_flags_risk(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Doc")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    local = await seed_warehouse(runtime_session_factory, tenant_id, code="CN-SZ", name="SZ")
    overseas = await seed_warehouse(
        runtime_session_factory, tenant_id, code="US-FBA", name="US FBA", country_code="US"
    )
    strip = await seed_variant(
        runtime_session_factory, tenant_id, sku="LED-COB-5M", product_name="COB Strip 5m"
    )
    steady = await seed_variant(runtime_session_factory, tenant_id, sku="DRIVER-60W")
    warning = await seed_variant(runtime_session_factory, tenant_id, sku="WARN-SKU")
    await seed_variant(runtime_session_factory, tenant_id, sku="IDLE-SKU")

    await seed_stock(runtime_session_factory, tenant_id, local, strip, 10)
    await seed_stock(runtime_session_factory, tenant_id, overseas, strip, 20)
    await seed_stock(runtime_session_factory, tenant_id, local, steady, 100)
    await seed_stock(runtime_session_factory, tenant_id, local, warning, 50)

    # Blended demand of exactly 10 units/day: 70 in 7d, 300 in 30d, 900 in 90d.
    await seed_order(runtime_session_factory, tenant_id, strip, quantity=70, days_ago=3)
    await seed_order(runtime_session_factory, tenant_id, strip, quantity=230, days_ago=20)
    await seed_order(runtime_session_factory, tenant_id, strip, quantity=600, days_ago=60)
    # Cancelled / refunded orders never count as demand.
    await seed_order(
        runtime_session_factory,
        tenant_id,
        strip,
        quantity=500,
        days_ago=5,
        fulfillment_status="cancelled",
    )
    await seed_order(
        runtime_session_factory,
        tenant_id,
        strip,
        quantity=400,
        days_ago=6,
        fulfillment_status="refunded",
    )
    # 5 units/day for the healthy band: 35 in 7d, 150 in 30d, 450 in 90d.
    await seed_order(runtime_session_factory, tenant_id, steady, quantity=35, days_ago=2)
    await seed_order(runtime_session_factory, tenant_id, steady, quantity=115, days_ago=20)
    await seed_order(runtime_session_factory, tenant_id, steady, quantity=300, days_ago=70)
    # 5 units/day against 50 units of cover = 10.00 DOC -> the warning band.
    await seed_order(runtime_session_factory, tenant_id, warning, quantity=35, days_ago=1)
    await seed_order(runtime_session_factory, tenant_id, warning, quantity=115, days_ago=20)
    await seed_order(runtime_session_factory, tenant_id, warning, quantity=300, days_ago=80)

    harness = build_harness(runtime_session_factory)
    harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
    async with harness.client() as client:
        # Put 5 strips in transit so the DOC model has to include the headhaul leg.
        created = await client.post(
            f"{API}/transfers",
            json={
                "source_warehouse_id": str(local),
                "destination_warehouse_id": str(overseas),
                "lines": [{"variant_id": str(strip), "qty": 5}],
            },
        )
        assert created.status_code == 201, created.text
        assert (
            await client.post(f"{API}/transfers/{created.json()['id']}/dispatch")
        ).status_code == 200

        doc = await client.get(f"{API}/doc-replenishment")
        assert doc.status_code == 200, doc.text
        body = doc.json()
        assert body["target_doc_days"] == 30
        rows = {row["sku"]: row for row in body["items"]}

        strip_row = rows["LED-COB-5M"]
        assert strip_row["product_name"] == "COB Strip 5m"
        assert strip_row["local_available_qty"] == 5  # 10 minus the 5 dispatched
        assert strip_row["overseas_available_qty"] == 20
        assert strip_row["in_transit_qty"] == 5
        assert strip_row["total_available_qty"] == 30
        assert strip_row["units_sold_7d"] == 70
        assert strip_row["units_sold_30d"] == 300
        assert strip_row["units_sold_90d"] == 900
        assert strip_row["weighted_daily_sales"] == "10.0000"
        assert strip_row["doc_days"] == "3.00"
        assert strip_row["suggested_reorder_qty"] == 270
        assert strip_row["risk_level"] == "critical"

        steady_row = rows["DRIVER-60W"]
        assert steady_row["weighted_daily_sales"] == "5.0000"
        assert steady_row["doc_days"] == "20.00"
        assert steady_row["suggested_reorder_qty"] == 50
        assert steady_row["risk_level"] == "healthy"

        warning_row = rows["WARN-SKU"]
        assert warning_row["weighted_daily_sales"] == "5.0000"
        assert warning_row["doc_days"] == "10.00"
        assert warning_row["suggested_reorder_qty"] == 100
        assert warning_row["risk_level"] == "warning"

        idle_row = rows["IDLE-SKU"]
        assert idle_row["weighted_daily_sales"] == "0.0000"
        assert idle_row["doc_days"] is None
        assert idle_row["suggested_reorder_qty"] == 0
        assert idle_row["risk_level"] == "healthy"

        assert body["critical_count"] == 1
        assert body["warning_count"] == 1
        # Critical cover is listed first so the worst SKU is never below the fold.
        assert body["items"][0]["sku"] == "LED-COB-5M"

        # The target DOC is configurable and changes the suggestion, not the sales blend.
        wider = await client.get(f"{API}/doc-replenishment?target_doc_days=60")
        assert wider.status_code == 200
        wider_rows = {row["sku"]: row for row in wider.json()["items"]}
        assert wider_rows["LED-COB-5M"]["suggested_reorder_qty"] == 570
        assert wider_rows["DRIVER-60W"]["suggested_reorder_qty"] == 200
        assert wider_rows["WARN-SKU"]["suggested_reorder_qty"] == 250
        assert wider_rows["LED-COB-5M"]["doc_days"] == "3.00"

        for invalid in (0, 366):
            rejected = await client.get(f"{API}/doc-replenishment?target_doc_days={invalid}")
            assert rejected.status_code == 422, invalid


# ----------------------------------------------------------------------------
# 5. RBAC + pure helper contracts
# ----------------------------------------------------------------------------


async def test_writes_require_stock_roles_but_every_role_can_read(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory, "Rbac")
    owner = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    agent = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    kit = await seed_variant(runtime_session_factory, tenant_id, sku="KIT-RBAC")
    part = await seed_variant(runtime_session_factory, tenant_id, sku="PART-RBAC")

    harness = build_harness(runtime_session_factory)
    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent, UserRole.AGENT))
        forbidden = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(part),
                "qty_per_unit": "1.0000",
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "ERR_INSUFFICIENT_ROLE"
        assert (await client.get(f"{API}/bom")).status_code == 200
        assert (await client.get(f"{API}/transfers")).status_code == 200
        assert (await client.get(f"{API}/doc-replenishment")).status_code == 200

        harness.act_as(principal(tenant_id, owner, UserRole.OWNER))
        allowed = await client.post(
            f"{API}/bom/components",
            json={
                "parent_variant_id": str(kit),
                "component_variant_id": str(part),
                "qty_per_unit": "1.0000",
            },
        )
        assert allowed.status_code == 201, allowed.text


def test_decimal_helpers_are_exact_and_round_half_up() -> None:
    assert bom_doc_service.effective_qty_per_unit(
        Decimal("2.5000"), Decimal("10.00")
    ) == Decimal("2.7500")
    assert bom_doc_service.effective_qty_per_unit(
        Decimal("1.0000"), Decimal("33.33")
    ) == Decimal("1.3333")
    assert bom_doc_service.weighted_daily_sales(70, 300, 900) == Decimal("10.0000")
    assert bom_doc_service.weighted_daily_sales(0, 0, 0) == Decimal("0.0000")
    assert bom_doc_service.doc_days(35, Decimal("10.0000")) == Decimal("3.50")
    assert bom_doc_service.doc_days(35, Decimal("0.0000")) is None
    assert bom_doc_service.suggested_reorder_qty(35, Decimal("10.0000"), 30) == 265
    assert bom_doc_service.suggested_reorder_qty(500, Decimal("10.0000"), 30) == 0
    assert bom_doc_service.risk_level(Decimal("6.99")) == "critical"
    assert bom_doc_service.risk_level(Decimal("7.00")) == "warning"
    assert bom_doc_service.risk_level(Decimal("14.99")) == "warning"
    assert bom_doc_service.risk_level(Decimal("15.00")) == "healthy"
    assert bom_doc_service.risk_level(None) == "healthy"
    assert bom_doc_service.ceil_units(Decimal("4.5")) == 5
    assert bom_doc_service.ceil_units(Decimal("4.0")) == 4


def test_warehouse_classification_prefers_explicit_overseas_markers() -> None:
    classify: Any = bom_doc_service.is_overseas_warehouse
    assert classify("CN", "CN-SZ", "Shenzhen Factory") is False
    assert classify("CN", "CN-FBA-PREP", "CN FBA Prep") is True
    assert classify("US", "US-LAX", "Los Angeles") is True
    assert classify(None, None, None) is False
