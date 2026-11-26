"""Wave 3 P1 closure: monotonic fulfillment state machine + inventory sync + oversell savepoints.

Covers three production defects:
* DS P1-7 / P2-23 - one oversold order in a reconciliation batch must never leave an orphan
  unreserved ``orders`` row behind, and the default warehouse must be a real tenant warehouse.
* GPT #16 / Muse P1-09 - externally-driven fulfillment updates are monotonic (never regress) and
  move stock (release on cancel, deduct on ship) idempotently through the order reference.
* GPT #16 / Muse P1-09 - an order imported for the first time already cancelled must not reserve.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register the domain model modules so SQLAlchemy can resolve cross-module string FKs.
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    EcommerceOrderSyncService,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.order_service import (
    OrderService,
    can_transition_fulfillment_status,
)
from erp_core.orders.reconciliation_worker import (
    reconcile_channel_account_orders,
    resolve_default_warehouse_id,
)
from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal, ShipOrderRequest
from erp_core.shared.db import create_session_factory, tenant_session
from erp_core.shared.enums import (
    ChannelType,
    FulfillmentStatus,
    OrderMode,
    PaymentStatus,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal

SKU = "W3C-STATE-MACHINE-SKU"
UNIT_PRICE = Decimal("19.9900")
UNIT_COST = Decimal("30.0000")


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:5432/erp_saas",
    )


def _admin_url() -> str:
    return os.getenv(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


class _StaticAdapter(CommercePlatformAdapter):
    """Deterministic stand-in for a storefront API (the DB effects are what we assert)."""

    def __init__(self, orders: Sequence[NormalizedOrder]) -> None:
        self._orders = list(orders)

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        del since
        if cursor is not None:
            return [], None
        return list(self._orders), None

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        del since, cursor
        return [], None

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, object]:
        del external_order_id, tracking_number, carrier_company, tracking_url
        del fulfillment_order_id, notify_customer
        return {}

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        del raw_body, headers, secret
        return False


class _Workspace:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        warehouse_id: UUID,
        variant_id: UUID,
        customer_id: UUID,
        channel_account_id: UUID | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.warehouse_id = warehouse_id
        self.variant_id = variant_id
        self.customer_id = customer_id
        self.channel_account_id = channel_account_id


class _PgHarness:
    """Seeds isolated tenants through the RLS-bound runtime role and purges them afterwards."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self._tenant_ids: list[UUID] = []

    async def seed(
        self,
        *,
        on_hand: int = 5,
        warehouses_row: bool = True,
        channel_account: bool = False,
        sku: str = SKU,
    ) -> _Workspace:
        tenant_id, warehouse_id, customer_id = uuid4(), uuid4(), uuid4()
        account_id = uuid4() if channel_account else None
        self._tenant_ids.append(tenant_id)
        async with tenant_session(self.factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'W3-C', :slug)"),
                {"tid": tenant_id, "slug": f"w3c-{tenant_id.hex[:12]}"},
            )
        async with tenant_session(self.factory, tenant_id) as session:
            if warehouses_row:
                await session.execute(
                    text(
                        "INSERT INTO warehouses (id, tenant_id, code, name, is_active) "
                        "VALUES (:wid, :tid, :code, 'Wave3 Main WH', true)"
                    ),
                    {"wid": warehouse_id, "tid": tenant_id, "code": f"WH-{warehouse_id.hex[:8]}"},
                )
            if account_id is not None:
                await session.execute(
                    text(
                        "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                        "external_account_id, display_name) VALUES "
                        "(:aid, :tid, 'shopify', :ext, 'Wave3 Store')"
                    ),
                    {"aid": account_id, "tid": tenant_id, "ext": f"w3c-{account_id.hex[:8]}"},
                )
            await session.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:cid, :tid, 'b2c_consumer', 'Wave3 Buyer')"
                ),
                {"cid": customer_id, "tid": tenant_id},
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code=f"SPU-W3C-{tenant_id.hex[:6]}", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku=sku,
                    cost_price_base=UNIT_COST,
                    retail_price_usd=UNIT_PRICE,
                    weight_grams=300,
                ),
            )
            if account_id is not None:
                await catalog.create_sku_mapping(
                    session,
                    tenant_id,
                    actor_id="seed",
                    payload=SkuMappingCreate(
                        variant_id=variant.id,
                        channel_account_id=account_id,
                        external_product_id="w3c_prod_1",
                        external_sku=sku,
                    ),
                )
            if on_hand > 0:
                await InventoryLedgerService().inbound_stock(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    variant_id=variant.id,
                    qty=on_hand,
                    reference_id=uuid4(),
                    idempotency_key=f"w3c-inbound-{variant.id}",
                )
        return _Workspace(
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant.id,
            customer_id=customer_id,
            channel_account_id=account_id,
        )

    async def purge(self) -> None:
        if not self._tenant_ids:
            return
        admin = create_async_engine(_admin_url())
        try:
            async with admin.begin() as conn:
                for tenant_id in self._tenant_ids:
                    for statement in (
                        "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                        "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                        "DELETE FROM shipments WHERE tenant_id = :tid",
                        "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                        "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                        "DELETE FROM order_items WHERE tenant_id = :tid",
                        "DELETE FROM orders WHERE tenant_id = :tid",
                        "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                        "DELETE FROM product_variants WHERE tenant_id = :tid",
                        "DELETE FROM products WHERE tenant_id = :tid",
                        "DELETE FROM customer_identities WHERE tenant_id = :tid",
                        "DELETE FROM customers WHERE tenant_id = :tid",
                        "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                        "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                        "DELETE FROM warehouses WHERE tenant_id = :tid",
                        "DELETE FROM audit_events WHERE tenant_id = :tid",
                    ):
                        await conn.execute(text(statement), {"tid": tenant_id})
                    await conn.execute(
                        text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id}
                    )
        finally:
            await admin.dispose()
        self._tenant_ids.clear()


@pytest.fixture
async def pg() -> AsyncIterator[_PgHarness]:
    engine = create_async_engine(_runtime_url())
    harness = _PgHarness(create_session_factory(engine))
    try:
        yield harness
    finally:
        await harness.purge()
        await engine.dispose()


def _order_payload(
    workspace: _Workspace,
    *,
    quantity: int = 2,
    external_order_id: str | None = None,
    initial_status: FulfillmentStatus = FulfillmentStatus.CONFIRMED_RESERVED,
) -> OrderCreateInternal:
    line_total = (UNIT_PRICE * quantity).quantize(Decimal("0.01"))
    return OrderCreateInternal(
        order_mode=OrderMode.B2C_RETAIL,
        customer_id=workspace.customer_id,
        warehouse_id=workspace.warehouse_id,
        channel_account_id=workspace.channel_account_id,
        external_order_id=external_order_id,
        currency="USD",
        fx_rate_to_base=Decimal("7.200000"),
        subtotal_amount=line_total,
        total_amount=line_total,
        initial_fulfillment_status=initial_status,
        items=[
            OrderItemInternal(
                variant_id=workspace.variant_id,
                sku_snapshot=SKU,
                quantity=quantity,
                unit_price=UNIT_PRICE,
                unit_cost_base_snapshot=UNIT_COST,
                line_total=line_total,
            )
        ],
    )


def _normalized_order(*, external_order_id: str, quantity: int) -> NormalizedOrder:
    line_total = (UNIT_PRICE * quantity).quantize(Decimal("0.01"))
    return NormalizedOrder(
        external_order_id=external_order_id,
        currency="USD",
        fx_rate_to_base=Decimal("7.200000"),
        subtotal_amount=line_total,
        total_amount=line_total,
        fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
        payment_status=PaymentStatus.PAID,
        customer=NormalizedCustomer(
            email=f"buyer-{external_order_id.lower()}@example.com", display_name="Wave3 Buyer"
        ),
        line_items=[
            NormalizedLineItem(
                external_sku=SKU, title="Wave3 Widget", quantity=quantity, line_total=line_total
            )
        ],
        updated_at=datetime.now(UTC),
    )


def _principal(tenant_id: UUID) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=uuid4(), tenant_id=tenant_id, role=UserRole.OWNER, email="ops@wave3.test"
    )


async def _balance(session: AsyncSession, workspace: _Workspace) -> tuple[int, int, int]:
    row = await InventoryLedgerService().get_balance(
        session, workspace.tenant_id, workspace.warehouse_id, workspace.variant_id
    )
    return (row.on_hand_qty, row.reserved_qty, row.available_qty)


# --------------------------------------------------------------------------------------
# Pure state-machine contract
# --------------------------------------------------------------------------------------
def test_can_transition_fulfillment_status_is_forward_only() -> None:
    status = FulfillmentStatus

    # Forward ladder is allowed.
    assert can_transition_fulfillment_status(status.PENDING_PAYMENT, status.CONFIRMED_RESERVED)
    assert can_transition_fulfillment_status(status.CONFIRMED_RESERVED, status.IN_PRODUCTION)
    assert can_transition_fulfillment_status(status.CONFIRMED_RESERVED, status.SHIPPED)
    assert can_transition_fulfillment_status(status.IN_PRODUCTION, status.SHIPPED)
    assert can_transition_fulfillment_status(status.SHIPPED, status.DELIVERED)
    # Cancellation / refund are reachable from any non-terminal state.
    assert can_transition_fulfillment_status(status.PENDING_PAYMENT, status.CANCELLED)
    assert can_transition_fulfillment_status(status.CONFIRMED_RESERVED, status.CANCELLED)
    assert can_transition_fulfillment_status(status.SHIPPED, status.REFUNDED)
    # Idempotent no-op.
    assert can_transition_fulfillment_status(status.SHIPPED, status.SHIPPED)
    assert can_transition_fulfillment_status(status.CANCELLED, status.CANCELLED)

    # No downgrades, ever.
    assert not can_transition_fulfillment_status(status.SHIPPED, status.CONFIRMED_RESERVED)
    assert not can_transition_fulfillment_status(status.DELIVERED, status.SHIPPED)
    assert not can_transition_fulfillment_status(status.DELIVERED, status.CONFIRMED_RESERVED)
    assert not can_transition_fulfillment_status(status.CONFIRMED_RESERVED, status.PENDING_PAYMENT)
    # Terminals are absorbing: a cancelled/refunded order never comes back to life.
    assert not can_transition_fulfillment_status(status.CANCELLED, status.CONFIRMED_RESERVED)
    assert not can_transition_fulfillment_status(status.CANCELLED, status.SHIPPED)
    assert not can_transition_fulfillment_status(status.REFUNDED, status.SHIPPED)
    assert not can_transition_fulfillment_status(status.REFUNDED, status.CANCELLED)


# --------------------------------------------------------------------------------------
# ship_order double-ship guard
# --------------------------------------------------------------------------------------
async def test_ship_order_rejects_second_shipment_with_409(pg: _PgHarness) -> None:
    workspace = await pg.seed(on_hand=5)
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        orders = OrderService()
        order = await orders.create_order_atomic(
            session,
            tenant_id=workspace.tenant_id,
            actor_type="human_user",
            actor_id="wave3-ops",
            payload=_order_payload(workspace),
        )
        principal = _principal(workspace.tenant_id)
        shipped = await orders.ship_order(
            session,
            workspace.tenant_id,
            principal,
            order.id,
            ShipOrderRequest(warehouse_id=workspace.warehouse_id, tracking_number="W3C-TRK-0001"),
        )
        assert shipped.tracking_number == "W3C-TRK-0001"
        assert await _balance(session, workspace) == (3, 0, 3)

        with pytest.raises(AppError) as rejected:
            await orders.ship_order(
                session,
                workspace.tenant_id,
                principal,
                order.id,
                ShipOrderRequest(
                    warehouse_id=workspace.warehouse_id, tracking_number="W3C-TRK-0002"
                ),
            )
        assert rejected.value.status_code == 409
        assert rejected.value.code == "ERR_ORDER_ALREADY_SHIPPED"


# --------------------------------------------------------------------------------------
# External fulfillment updates: monotonic + inventory sync
# --------------------------------------------------------------------------------------
async def test_external_cancel_releases_stock_and_late_update_cannot_regress(
    pg: _PgHarness,
) -> None:
    workspace = await pg.seed(on_hand=5)
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        orders = OrderService()
        tenant_id = workspace.tenant_id

        cancellable = await orders.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id="wave3-sync",
            payload=_order_payload(workspace, quantity=2, external_order_id="W3C-EXT-CANCEL"),
        )
        assert await _balance(session, workspace) == (5, 2, 3)

        cancelled = await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=cancellable.id,
            fulfillment_status=FulfillmentStatus.CANCELLED,
        )
        assert cancelled.fulfillment_status == FulfillmentStatus.CANCELLED
        # Reservation handed back to available stock.
        assert await _balance(session, workspace) == (5, 0, 5)

        shippable = await orders.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id="wave3-sync",
            payload=_order_payload(workspace, quantity=2, external_order_id="W3C-EXT-SHIP"),
        )
        assert await _balance(session, workspace) == (5, 2, 3)

        shipped = await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=shippable.id,
            fulfillment_status=FulfillmentStatus.SHIPPED,
        )
        assert shipped.fulfillment_status == FulfillmentStatus.SHIPPED
        # Shipped units left the warehouse: on_hand and reserved both dropped.
        assert await _balance(session, workspace) == (3, 0, 3)

        regressed = await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=shippable.id,
            fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
        )
        assert regressed.fulfillment_status == FulfillmentStatus.SHIPPED
        assert await _balance(session, workspace) == (3, 0, 3)

        # A repeated cancel webhook is an idempotent no-op (no double release).
        await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=cancellable.id,
            fulfillment_status=FulfillmentStatus.CANCELLED,
        )
        assert await _balance(session, workspace) == (3, 0, 3)


async def test_external_full_refund_releases_reserved_stock_idempotently(
    pg: _PgHarness,
) -> None:
    workspace = await pg.seed(on_hand=5)
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        orders = OrderService()
        tenant_id = workspace.tenant_id

        refundable = await orders.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id="wave3-sync",
            payload=_order_payload(workspace, quantity=2, external_order_id="W3C-EXT-REFUND"),
        )
        assert await _balance(session, workspace) == (5, 2, 3)

        refunded = await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=refundable.id,
            fulfillment_status=FulfillmentStatus.REFUNDED,
            payment_status=PaymentStatus.REFUNDED,
        )
        assert refunded.fulfillment_status == FulfillmentStatus.REFUNDED
        assert await _balance(session, workspace) == (5, 0, 5)

        # Repeated refund webhook must remain an idempotent no-op (no double release).
        await orders.update_external_order_status(
            session,
            tenant_id=tenant_id,
            order_id=refundable.id,
            fulfillment_status=FulfillmentStatus.REFUNDED,
            payment_status=PaymentStatus.REFUNDED,
        )
        assert await _balance(session, workspace) == (5, 0, 5)


async def test_first_import_of_cancelled_order_does_not_reserve_stock(pg: _PgHarness) -> None:
    workspace = await pg.seed(on_hand=5, channel_account=True)
    assert workspace.channel_account_id is not None
    dto = _normalized_order(external_order_id="W3C-ALREADY-CANCELLED", quantity=2).model_copy(
        update={"fulfillment_status": FulfillmentStatus.CANCELLED}
    )
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        order_id, created = await EcommerceOrderSyncService().ingest_normalized_order(
            session,
            tenant_id=workspace.tenant_id,
            channel_account_id=workspace.channel_account_id,
            default_warehouse_id=workspace.warehouse_id,
            order_dto=dto,
        )
        assert created is True
        order = await OrderService().get_order(session, workspace.tenant_id, order_id)
        assert order.fulfillment_status == FulfillmentStatus.CANCELLED
        assert await _balance(session, workspace) == (5, 0, 5)


# --------------------------------------------------------------------------------------
# Reconciliation savepoint isolation (DS P1-7 / P2-23)
# --------------------------------------------------------------------------------------
async def test_reconciliation_batch_commits_valid_order_and_leaves_no_orphan_rows(
    pg: _PgHarness,
) -> None:
    workspace = await pg.seed(on_hand=1, channel_account=True)
    assert workspace.channel_account_id is not None
    adapter = _StaticAdapter(
        [
            _normalized_order(external_order_id="W3C-BATCH-OK", quantity=1),
            _normalized_order(external_order_id="W3C-BATCH-OVERSOLD", quantity=5),
        ]
    )
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        stats = await reconcile_channel_account_orders(
            session,
            tenant_id=workspace.tenant_id,
            channel_account_id=workspace.channel_account_id,
            channel_type=ChannelType.SHOPIFY,
            adapter=adapter,
            default_warehouse_id=workspace.warehouse_id,
            since=datetime.now(UTC) - timedelta(minutes=30),
        )
        assert stats["recovered"] == 1
        assert stats["updated"] == 0
        assert stats["failed"] == 1
        assert await _balance(session, workspace) == (1, 1, 0)

    # Fresh transaction: the valid order committed, the oversold one left nothing behind.
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT o.external_order_id, COUNT(i.id) AS item_count FROM orders o "
                    "LEFT JOIN order_items i ON i.order_id = o.id AND i.tenant_id = o.tenant_id "
                    "WHERE o.tenant_id = :tid GROUP BY o.external_order_id"
                ),
                {"tid": workspace.tenant_id},
            )
        ).all()
        assert [(row.external_order_id, int(row.item_count)) for row in rows] == [
            ("W3C-BATCH-OK", 1)
        ]
        orphan_orders = await session.scalar(
            text(
                "SELECT COUNT(*) FROM orders WHERE tenant_id = :tid "
                "AND external_order_id = 'W3C-BATCH-OVERSOLD'"
            ),
            {"tid": workspace.tenant_id},
        )
        assert orphan_orders == 0
        assert await _balance(session, workspace) == (1, 1, 0)


async def test_reconciliation_savepoint_rolls_back_a_partially_ingested_order(
    pg: _PgHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the order row is written must still leave nothing behind.

    The oversold case is caught by the ingestion savepoint; this one deliberately explodes
    *after* a successful write (a downstream finance/profit failure), which is exactly what the
    per-order savepoint in the reconciliation loop is there to contain.
    """
    workspace = await pg.seed(on_hand=5, channel_account=True)
    assert workspace.channel_account_id is not None
    original = EcommerceOrderSyncService.ingest_normalized_order

    async def flaky_ingest(
        self: EcommerceOrderSyncService,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        default_warehouse_id: UUID,
        order_dto: NormalizedOrder,
        actor_id: str = "ecommerce_sync",
    ) -> tuple[UUID, bool]:
        result = await original(
            self,
            session,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            default_warehouse_id=default_warehouse_id,
            order_dto=order_dto,
            actor_id=actor_id,
        )
        if order_dto.external_order_id == "W3C-LATE-FAIL":
            raise AppError(
                slug="downstream-finance-failed",
                status_code=500,
                code="ERR_DOWNSTREAM_FINANCE_FAILED",
                title="Downstream Failure",
                detail="Simulated post-write failure inside the reconciliation batch.",
            )
        return result

    monkeypatch.setattr(EcommerceOrderSyncService, "ingest_normalized_order", flaky_ingest)
    adapter = _StaticAdapter(
        [
            _normalized_order(external_order_id="W3C-BATCH-OK", quantity=1),
            _normalized_order(external_order_id="W3C-LATE-FAIL", quantity=2),
        ]
    )
    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        stats = await reconcile_channel_account_orders(
            session,
            tenant_id=workspace.tenant_id,
            channel_account_id=workspace.channel_account_id,
            channel_type=ChannelType.SHOPIFY,
            adapter=adapter,
            default_warehouse_id=workspace.warehouse_id,
            since=datetime.now(UTC) - timedelta(minutes=30),
        )
        assert (stats["recovered"], stats["failed"]) == (1, 1)

    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        external_ids = (
            await session.execute(
                text("SELECT external_order_id FROM orders WHERE tenant_id = :tid"),
                {"tid": workspace.tenant_id},
            )
        ).scalars().all()
        assert list(external_ids) == ["W3C-BATCH-OK"]
        # The failed order's reservation came back with its savepoint: only the good order
        # holds one unit.
        assert await _balance(session, workspace) == (5, 1, 4)


# --------------------------------------------------------------------------------------
# Real default warehouse resolution (DS P2-23)
# --------------------------------------------------------------------------------------
async def test_default_warehouse_resolution_prefers_active_tenant_warehouse(
    pg: _PgHarness,
) -> None:
    workspace = await pg.seed(on_hand=1, channel_account=True)
    legacy = await pg.seed(on_hand=0, warehouses_row=False, channel_account=True)
    assert workspace.channel_account_id is not None
    assert legacy.channel_account_id is not None

    async with tenant_session(pg.factory, workspace.tenant_id) as session:
        resolved = await resolve_default_warehouse_id(
            session,
            tenant_id=workspace.tenant_id,
            channel_account_id=workspace.channel_account_id,
            account_config={},
            warehouse_override=None,
        )
        assert resolved == workspace.warehouse_id
        assert resolved != workspace.channel_account_id

        override = uuid4()
        assert (
            await resolve_default_warehouse_id(
                session,
                tenant_id=workspace.tenant_id,
                channel_account_id=workspace.channel_account_id,
                account_config=None,
                warehouse_override=override,
            )
            == override
        )
        configured = uuid4()
        assert (
            await resolve_default_warehouse_id(
                session,
                tenant_id=workspace.tenant_id,
                channel_account_id=workspace.channel_account_id,
                account_config={"warehouse_id": str(configured)},
                warehouse_override=override,
            )
            == configured
        )

    # Legacy tenant with no warehouses rows at all keeps the historical account-id fallback.
    async with tenant_session(pg.factory, legacy.tenant_id) as session:
        fallback = await resolve_default_warehouse_id(
            session,
            tenant_id=legacy.tenant_id,
            channel_account_id=legacy.channel_account_id,
            account_config={},
            warehouse_override=None,
        )
        assert fallback == legacy.channel_account_id
