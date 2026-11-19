from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.models import Order, OrderItem, Shipment
from erp_core.orders.schemas import (
    OrderCreateInternal,
    OrderCreateRequest,
    OrderItemInternal,
    OrderItemRead,
    OrderRead,
    ShipmentRead,
    ShipOrderRequest,
)
from erp_core.shared.enums import (
    FulfillmentStatus,
    OrderMode,
    PaymentStatus,
    TrackingProvider,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class TrackingRegistrationPort(Protocol):
    async def register_tracking(
        self, tracking_number: str, carrier_code: str | None = None
    ) -> dict[str, Any]: ...


class OrderService:
    """Unified B2C + B2B order lifecycle with atomic inventory and payment milestone gates."""

    def __init__(
        self,
        inventory_service: InventoryLedgerService | None = None,
        catalog_service: CatalogService | None = None,
    ) -> None:
        self.inventory = inventory_service or InventoryLedgerService()
        self.catalog = catalog_service or CatalogService()

    async def _serialize_order(
        self, session: AsyncSession, tenant_id: UUID, order: Order
    ) -> OrderRead:
        items = (
            (
                await session.execute(
                    select(OrderItem).where(
                        OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id
                    )
                )
            )
            .scalars()
            .all()
        )
        ship_rows = (
            (
                await session.execute(
                    select(Shipment)
                    .where(Shipment.tenant_id == tenant_id, Shipment.order_id == order.id)
                    .order_by(Shipment.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        serialized_shipments: list[ShipmentRead] = []
        for s in ship_rows:
            stagnant = bool(
                s.exception_alert_sent
                or (
                    s.standardized_status not in ("delivered", "cancelled", "expired")
                    and s.last_event_at is not None
                    and (now - s.last_event_at) >= timedelta(hours=72)
                )
            )
            sr = ShipmentRead.model_validate(s)
            serialized_shipments.append(sr.model_copy(update={"is_stagnant_72h": stagnant}))
        cust_name = (
            await session.execute(
                text("SELECT display_name FROM customers WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": order.customer_id},
            )
        ).scalar_one_or_none()
        return OrderRead(
            id=order.id,
            tenant_id=order.tenant_id,
            order_no=order.order_no,
            order_mode=order.order_mode,
            channel_account_id=order.channel_account_id,
            external_order_id=order.external_order_id,
            customer_id=order.customer_id,
            customer_name=cust_name,
            quote_id=order.quote_id,
            currency=order.currency,
            fx_rate_to_base=Decimal(str(order.fx_rate_to_base)),
            subtotal_amount=Decimal(str(order.subtotal_amount)),
            shipping_charged=Decimal(str(order.shipping_charged)),
            discount_amount=Decimal(str(order.discount_amount)),
            tax_collected=Decimal(str(order.tax_collected)),
            total_amount=Decimal(str(order.total_amount)),
            fulfillment_status=order.fulfillment_status,
            payment_status=order.payment_status,
            channel_metadata=dict(order.channel_metadata or {}),
            has_72h_stagnation_alert=any(sh.is_stagnant_72h for sh in serialized_shipments),
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=[OrderItemRead.model_validate(i) for i in items],
            shipments=serialized_shipments,
        )

    async def create_manual_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: OrderCreateRequest,
    ) -> OrderRead:
        internal_items: list[OrderItemInternal] = []
        subtotal = Decimal("0.00")
        for line in payload.items:
            vp = await self.catalog.get_variant_pricing_internal(
                session, tenant_id, line.variant_id
            )
            unit_price = (
                line.unit_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if line.unit_price is not None
                else vp.retail_price_usd
            )
            line_total = (Decimal(line.quantity) * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            subtotal += line_total
            internal_items.append(
                OrderItemInternal(
                    variant_id=vp.id,
                    sku_snapshot=vp.sku,
                    quantity=line.quantity,
                    unit_price=unit_price,
                    unit_cost_base_snapshot=vp.cost_price_base,
                    line_total=line_total,
                )
            )

        total = (
            subtotal + payload.shipping_charged - payload.discount_amount + payload.tax_collected
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        initial_status = (
            FulfillmentStatus.PENDING_PAYMENT
            if payload.order_mode in {OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE}
            else FulfillmentStatus.CONFIRMED_RESERVED
        )
        return await self.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            payload=OrderCreateInternal(
                order_mode=payload.order_mode,
                customer_id=payload.customer_id,
                warehouse_id=payload.warehouse_id,
                currency=payload.currency.upper(),
                fx_rate_to_base=payload.fx_rate_to_base,
                subtotal_amount=subtotal,
                shipping_charged=payload.shipping_charged,
                discount_amount=payload.discount_amount,
                tax_collected=payload.tax_collected,
                total_amount=total,
                initial_fulfillment_status=initial_status,
                items=internal_items,
            ),
        )

    async def create_order_atomic(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_type: Literal["human_user", "ai_agent", "system_cron", "webhook"],
        actor_id: str,
        payload: OrderCreateInternal,
    ) -> OrderRead:
        now = datetime.now(UTC)
        order_no = payload.order_no_override or f"ORD-{now:%Y%m%d}-{uuid4().hex[:6].upper()}"
        order = Order(
            tenant_id=tenant_id,
            order_no=order_no,
            order_mode=payload.order_mode,
            channel_account_id=payload.channel_account_id,
            external_order_id=payload.external_order_id,
            customer_id=payload.customer_id,
            quote_id=payload.quote_id,
            currency=payload.currency.upper(),
            fx_rate_to_base=payload.fx_rate_to_base.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            subtotal_amount=payload.subtotal_amount.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            shipping_charged=payload.shipping_charged.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            discount_amount=payload.discount_amount.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            tax_collected=payload.tax_collected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            total_amount=payload.total_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            fulfillment_status=payload.initial_fulfillment_status,
            payment_status=payload.initial_payment_status,
            channel_metadata=payload.channel_metadata,
        )
        session.add(order)
        await session.flush()

        for item in payload.items:
            session.add(
                OrderItem(
                    tenant_id=tenant_id,
                    order_id=order.id,
                    variant_id=item.variant_id,
                    sku_snapshot=item.sku_snapshot,
                    quantity=item.quantity,
                    unit_price=item.unit_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                    unit_cost_base_snapshot=item.unit_cost_base_snapshot.quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    ),
                    line_total=item.line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                )
            )
        await session.flush()

        reserve_pairs = [(item.variant_id, item.quantity) for item in payload.items]
        await self.inventory.reserve_for_order(
            session,
            tenant_id=tenant_id,
            warehouse_id=payload.warehouse_id,
            order_id=order.id,
            items=reserve_pairs,
            idempotency_prefix=f"order-reserve:{order.id}",
        )

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.created",
            resource_type="order",
            resource_id=order.id,
            after_state={
                "order_no": order.order_no,
                "order_mode": order.order_mode.value,
                "total_amount": str(order.total_amount),
            },
        )
        return await self._serialize_order(session, tenant_id, order)

    async def get_order(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> OrderRead:
        order = (
            await session.execute(
                select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id)
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )
        return await self._serialize_order(session, tenant_id, order)

    async def find_by_external_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        external_order_id: str,
    ) -> OrderRead | None:
        order = (
            await session.execute(
                select(Order).where(
                    Order.tenant_id == tenant_id,
                    Order.channel_account_id == channel_account_id,
                    Order.external_order_id == external_order_id,
                )
            )
        ).scalar_one_or_none()
        if order is None:
            return None
        return await self._serialize_order(session, tenant_id, order)

    async def list_orders(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_mode: OrderMode | None = None,
        fulfillment_status: FulfillmentStatus | None = None,
        payment_status: PaymentStatus | None = None,
        customer_id: UUID | None = None,
        search: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[OrderRead]:
        stmt = select(Order).where(Order.tenant_id == tenant_id)
        cnt = select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)
        if order_mode is not None:
            stmt = stmt.where(Order.order_mode == order_mode)
            cnt = cnt.where(Order.order_mode == order_mode)
        if fulfillment_status is not None:
            stmt = stmt.where(Order.fulfillment_status == fulfillment_status)
            cnt = cnt.where(Order.fulfillment_status == fulfillment_status)
        if payment_status is not None:
            stmt = stmt.where(Order.payment_status == payment_status)
            cnt = cnt.where(Order.payment_status == payment_status)
        if customer_id is not None:
            stmt = stmt.where(Order.customer_id == customer_id)
            cnt = cnt.where(Order.customer_id == customer_id)
        if search:
            pat = f"%{search.strip()}%"
            stmt = stmt.where(Order.order_no.ilike(pat) | Order.external_order_id.ilike(pat))
            cnt = cnt.where(Order.order_no.ilike(pat) | Order.external_order_id.ilike(pat))

        total = int((await session.execute(cnt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [await self._serialize_order(session, tenant_id, row) for row in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def cancel_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_id: UUID,
        warehouse_id: UUID,
        reason: str,
    ) -> OrderRead:
        order = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id, Order.id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )
        if order.fulfillment_status in {
            FulfillmentStatus.SHIPPED,
            FulfillmentStatus.DELIVERED,
            FulfillmentStatus.CANCELLED,
        }:
            raise AppError(
                slug="order-cannot-cancel",
                status_code=409,
                code="ERR_ORDER_CANNOT_CANCEL",
                title="Cannot Cancel Order",
                detail=(
                    f"Order '{order.order_no}' in status "
                    f"'{order.fulfillment_status}' cannot be cancelled."
                ),
            )

        items = (
            (
                await session.execute(
                    select(OrderItem).where(
                        OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id
                    )
                )
            )
            .scalars()
            .all()
        )
        await self.inventory.release_reservation(
            session,
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            order_id=order.id,
            items=[(i.variant_id, i.quantity) for i in items],
            idempotency_prefix=f"order-cancel:{order.id}",
        )
        prev_status = order.fulfillment_status.value
        order.fulfillment_status = FulfillmentStatus.CANCELLED
        order.updated_at = datetime.now(UTC)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="order.cancelled",
            resource_type="order",
            resource_id=order.id,
            before_state={"fulfillment_status": prev_status},
            after_state={"fulfillment_status": order.fulfillment_status.value, "reason": reason},
        )
        return await self._serialize_order(session, tenant_id, order)

    async def ship_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        order_id: UUID,
        payload: ShipOrderRequest,
        tracking_adapter: TrackingRegistrationPort | None = None,
    ) -> ShipmentRead:
        order = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id, Order.id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )
        if order.fulfillment_status in {FulfillmentStatus.CANCELLED, FulfillmentStatus.REFUNDED}:
            raise AppError(
                slug="order-inactive",
                status_code=409,
                code="ERR_ORDER_INACTIVE",
                title="Order Inactive",
                detail="Cannot ship cancelled/refunded order.",
            )

        # Enforce 70% balance gate on B2B wholesale orders
        if order.order_mode == OrderMode.B2B_WHOLESALE:
            unsettled_count = int(
                (
                    await session.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM receivables_payables
                            WHERE tenant_id = :tid AND order_id = :oid
                              AND direction = 'receivable'
                              AND status IN ('open', 'partial', 'overdue')
                            """
                        ),
                        {"tid": tenant_id, "oid": order.id},
                    )
                ).scalar_one()
            )
            if order.payment_status != PaymentStatus.PAID or unsettled_count > 0:
                if not payload.override_unpaid_balance:
                    raise AppError(
                        slug="balance-payment-required",
                        status_code=409,
                        code="ERR_BALANCE_PAYMENT_REQUIRED",
                        title="Balance Payment Required",
                        detail=(
                            "B2B wholesale order cannot ship until 70% balance receivable "
                            "is settled (or Owner/Finance override)."
                        ),
                    )
                if principal.role not in {UserRole.OWNER, UserRole.FINANCE}:
                    raise AppError(
                        slug="insufficient-role",
                        status_code=403,
                        code="ERR_INSUFFICIENT_ROLE",
                        title="Override Forbidden",
                        detail=(
                            "Only Owner or Finance may authorize shipping with an unpaid balance."
                        ),
                    )
                if not payload.override_reason or not payload.override_reason.strip():
                    raise AppError(
                        slug="override-reason-required",
                        status_code=422,
                        code="ERR_OVERRIDE_REASON_REQUIRED",
                        title="Override Reason Required",
                        detail="override_reason is mandatory when overriding unpaid balance gate.",
                    )

        items = (
            (
                await session.execute(
                    select(OrderItem).where(
                        OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id
                    )
                )
            )
            .scalars()
            .all()
        )

        shipment = Shipment(
            tenant_id=tenant_id,
            order_id=order.id,
            tracking_provider=payload.tracking_provider,
            tracking_number=payload.tracking_number.strip(),
            carrier_code=payload.carrier_code,
            shipment_mode=payload.shipment_mode,
            standardized_status="info_received",
            actual_shipping_cost_base=payload.actual_shipping_cost_base.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
        )
        session.add(shipment)
        await session.flush()

        await self.inventory.deduct_shipped(
            session,
            tenant_id=tenant_id,
            warehouse_id=payload.warehouse_id,
            shipment_id=shipment.id,
            items=[(i.variant_id, i.quantity) for i in items],
            idempotency_prefix=f"order-ship:{shipment.id}",
        )

        if tracking_adapter is not None and payload.tracking_provider == TrackingProvider(
            "17track"
        ):
            await tracking_adapter.register_tracking(
                shipment.tracking_number, shipment.carrier_code
            )

        prev_status = order.fulfillment_status.value
        order.fulfillment_status = FulfillmentStatus.SHIPPED
        order.updated_at = datetime.now(UTC)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="shipment.unpaid_balance_override"
            if payload.override_unpaid_balance
            else "order.shipped",
            resource_type="order",
            resource_id=order.id,
            before_state={"fulfillment_status": prev_status},
            after_state={
                "fulfillment_status": order.fulfillment_status.value,
                "tracking_number": shipment.tracking_number,
                "override_reason": payload.override_reason,
            },
        )
        return ShipmentRead.model_validate(shipment)

    async def apply_payment_milestone_state(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        new_payment_status: PaymentStatus,
        unlock_production: bool = False,
    ) -> OrderRead:
        order = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id, Order.id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )

        order.payment_status = new_payment_status
        if unlock_production and order.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT:
            order.fulfillment_status = (
                FulfillmentStatus.IN_PRODUCTION
                if order.order_mode in {OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE}
                else FulfillmentStatus.CONFIRMED_RESERVED
            )
        order.updated_at = datetime.now(UTC)
        await session.flush()
        return await self._serialize_order(session, tenant_id, order)

    async def update_external_order_status(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        fulfillment_status: FulfillmentStatus | None = None,
        payment_status: PaymentStatus | None = None,
        channel_metadata_patch: dict[str, Any] | None = None,
    ) -> OrderRead:
        order = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id, Order.id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            raise AppError(
                slug="order-not-found",
                status_code=404,
                code="ERR_ORDER_NOT_FOUND",
                title="Order Not Found",
                detail=f"Order '{order_id}' not found.",
            )
        if fulfillment_status is not None:
            order.fulfillment_status = fulfillment_status
        if payment_status is not None:
            order.payment_status = payment_status
        if channel_metadata_patch:
            merged = dict(order.channel_metadata or {})
            merged.update(channel_metadata_patch)
            order.channel_metadata = merged
        order.updated_at = datetime.now(UTC)
        await session.flush()
        return await self._serialize_order(session, tenant_id, order)
