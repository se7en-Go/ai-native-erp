from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from erp_schema_bootstrap import ensure_erp_schema, register_plan02_metadata_stand_ins
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.orders.inventory_service import InsufficientStockError, InventoryLedgerService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, UserRole


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_50_concurrent_reservations_for_last_unit_no_oversell() -> None:
    await ensure_erp_schema(_migrator_url())
    register_plan02_metadata_stand_ins()
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    # Use pool_size >= 55 so all 50 coroutines genuinely contend concurrently in PostgreSQL
    rt_engine = create_async_engine(_runtime_url(), echo=False, pool_size=55, max_overflow=10)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    customer_id = uuid4()
    warehouse_id = uuid4()

    try:
        # tenants / customers are FORCE-RLS protected: seed through app_runtime.
        async with tenant_session(rt_factory, tenant_id) as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Race Test Co", "slug": f"race-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:id, :tid, 'b2c_consumer', 'Flash Buyer')"
                ),
                {"id": customer_id, "tid": tenant_id},
            )

        catalog_svc = CatalogService()
        inv_svc = InventoryLedgerService()

        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-LED-LAST", category="lighting"),
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("30.0000"),
                    retail_price_usd=Decimal("19.99"),
                    weight_grams=300,
                ),
            )
            await inv_svc.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant.id,
                qty=1,
                reference_id=uuid4(),
                idempotency_key=f"inbound-last-1-{variant.id}",
            )

        async def _attempt_reserve(idx: int) -> str:
            async with tenant_session(rt_factory, tenant_id) as sess:
                await inv_svc.reserve_for_order(
                    sess,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    order_id=uuid4(),
                    items=[(variant.id, 1)],
                    idempotency_prefix=f"race-res-{idx}",
                )
            return "reserved"

        results = await asyncio.gather(
            *[_attempt_reserve(i) for i in range(50)], return_exceptions=True
        )
        successes = [r for r in results if r == "reserved"]
        stock_failures = [r for r in results if isinstance(r, InsufficientStockError)]
        unexpected = [
            r for r in results if r != "reserved" and not isinstance(r, InsufficientStockError)
        ]

        assert unexpected == [], f"Unexpected exceptions: {unexpected}"
        assert len(successes) == 1
        assert len(stock_failures) == 49

        async with tenant_session(rt_factory, tenant_id) as session:
            bal = await inv_svc.get_balance(session, tenant_id, warehouse_id, variant.id)
            assert bal.on_hand_qty == 1
            assert bal.reserved_qty == 1
            assert bal.available_qty == 0

            ledger_sums = (
                await session.execute(
                    text(
                        """
                        SELECT
                            COALESCE(SUM(qty_delta_on_hand), 0) AS sum_on_hand,
                            COALESCE(SUM(qty_delta_reserved), 0) AS sum_reserved
                        FROM inventory_ledger
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                )
            ).one()
            assert ledger_sums.sum_on_hand == bal.on_hand_qty == 1
            assert ledger_sums.sum_reserved == bal.reserved_qty == 1

            # Verify idempotent order creation + cancellation releases stock back to 1
            await inv_svc.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant.id,
                qty=5,
                reference_id=uuid4(),
                idempotency_key=f"inbound-more-{variant.id}",
            )
            order_svc = OrderService(inventory_service=inv_svc)
            order = await order_svc.create_order_atomic(
                session,
                tenant_id=tenant_id,
                actor_type="human_user",
                actor_id=str(uuid4()),
                payload=OrderCreateInternal(
                    order_mode=OrderMode.B2C_RETAIL,
                    customer_id=customer_id,
                    warehouse_id=warehouse_id,
                    currency="USD",
                    fx_rate_to_base=Decimal("7.200000"),
                    subtotal_amount=Decimal("39.98"),
                    total_amount=Decimal("39.98"),
                    initial_fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
                    items=[
                        OrderItemInternal(
                            variant_id=variant.id,
                            sku_snapshot="LED-STRIP-5M-IP65",
                            quantity=2,
                            unit_price=Decimal("19.9900"),
                            unit_cost_base_snapshot=Decimal("30.0000"),
                            line_total=Decimal("39.98"),
                        )
                    ],
                ),
            )
            bal_after_order = await inv_svc.get_balance(
                session, tenant_id, warehouse_id, variant.id
            )
            assert bal_after_order.on_hand_qty == 6
            assert bal_after_order.reserved_qty == 3
            assert bal_after_order.available_qty == 3

            cancelled = await order_svc.cancel_order(
                session,
                tenant_id=tenant_id,
                actor_id="ops-user",
                order_id=order.id,
                warehouse_id=warehouse_id,
                reason="buyer cancelled",
            )
            assert cancelled.fulfillment_status == FulfillmentStatus.CANCELLED
            bal_after_cancel = await inv_svc.get_balance(
                session, tenant_id, warehouse_id, variant.id
            )
            assert bal_after_cancel.reserved_qty == 1
            assert bal_after_cancel.available_qty == 5
    finally:
        # `inventory_ledger` is append-only (DELETE revoked from app_runtime), so its rows
        # and the products/variants/tenant they point at are intentionally retained.
        async with tenant_session(rt_factory, tenant_id) as conn:
            for stmt in (
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
            ):
                await conn.execute(text(stmt), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
