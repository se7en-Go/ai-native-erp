from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.api_main import app as _app  # noqa: F401
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import ShipOrderRequest
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, PaymentStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal


async def test_verified_full_deposit_unlocks_production_and_only_full_balance_unlocks_shipping(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from erp_core.finance.schemas import PaymentCreate

    tid, cid, oid, warehouse, pid, variant = (uuid4() for _ in range(6))
    finance = CurrentPrincipal(
        user_id=uuid4(), tenant_id=tid, role=UserRole.FINANCE, email="finance@example.com"
    )
    agent = finance.model_copy(update={"role": UserRole.AGENT})
    orders, payments, profits = OrderService(), PaymentService(), OrderProfitService()

    async with tenant_session(runtime_session_factory, tid) as session:
        try:
            await session.execute(
                text(
                    "INSERT INTO tenants (id,name,slug,base_currency) "
                    "VALUES (:tid,'Finance',:slug,'CNY')"
                ),
                {"tid": tid, "slug": f"f-{tid.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO customers (id,tenant_id,customer_type,"
                    "display_name) VALUES (:cid,:tid,'b2b_contact','Buyer')"
                ),
                {"cid": cid, "tid": tid},
            )
            await session.execute(
                text(
                    "INSERT INTO orders (id,tenant_id,customer_id,order_no,"
                    "order_mode,currency,fx_rate_to_base,subtotal_amount,total_amount, "
                    "fulfillment_status,payment_status) VALUES "
                    "(:oid,:tid,:cid,'B2B-CVP','b2b_wholesale','USD',7.2,1000,1000, "
                    "'pending_payment','unpaid')"
                ),
                {"oid": oid, "tid": tid, "cid": cid},
            )
            await session.execute(
                text(
                    "INSERT INTO products (id,tenant_id,spu_code,category) "
                    "VALUES (:pid,:tid,'CVP','lighting')"
                ),
                {"pid": pid, "tid": tid},
            )
            await session.execute(
                text(
                    "INSERT INTO product_variants (id,tenant_id,product_id,"
                    "sku,cost_price_base,retail_price_usd,weight_grams) "
                    "VALUES (:vid,:tid,:pid,'CVP-SKU',40,10,100)"
                ),
                {"vid": variant, "tid": tid, "pid": pid},
            )
            await session.execute(
                text(
                    "INSERT INTO order_items (tenant_id,order_id,variant_id,"
                    "sku_snapshot,quantity,unit_price,unit_cost_base_snapshot,line_total) VALUES "
                    "(:tid,:oid,:vid,'CVP-SKU',100,10,40,1000)"
                ),
                {"tid": tid, "oid": oid, "vid": variant},
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id,warehouse_id,"
                    "variant_id,on_hand_qty,reserved_qty) VALUES (:tid,:wid,:vid,100,100)"
                ),
                {"tid": tid, "wid": warehouse, "vid": variant},
            )
            deposit, balance = await payments.create_b2b_milestone_receivables(
                session,
                tid,
                oid,
                Decimal("1000"),
                "USD",
            )
            assert (deposit.due_amount, balance.due_amount) == (Decimal("300"), Decimal("700"))
            slip = PaymentCreate(
                rp_id=deposit.id,
                amount_received=Decimal("299.99"),
                currency="USD",
                payment_method="tt_wire",
                actual_fx_rate=Decimal("7.25"),
                gateway_fee=Decimal("15"),
            )
            with pytest.raises(AppError) as denied:
                await payments.record_and_verify_payment(session, tid, agent, slip)
            assert denied.value.status_code == 403

            pending = await payments.submit_payment_receipt(session, tid, finance, slip)
            assert (
                await orders.get_order(session, tid, oid)
            ).payment_status == PaymentStatus.UNPAID
            await payments.verify_payment(session, tid, finance, pending.id)
            partial = await orders.get_order(session, tid, oid)
            assert partial.payment_status == PaymentStatus.PARTIALLY_PAID
            assert partial.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT
            await payments.record_and_verify_payment(
                session,
                tid,
                finance,
                slip.model_copy(
                    update={"amount_received": Decimal("0.01"), "gateway_fee": Decimal(0)}
                ),
            )
            full_deposit = await orders.get_order(session, tid, oid)
            assert full_deposit.payment_status == PaymentStatus.DEPOSIT_PAID
            assert full_deposit.fulfillment_status == FulfillmentStatus.IN_PRODUCTION

            ship = ShipOrderRequest(
                warehouse_id=warehouse,
                tracking_number="CVP-TEST",
                actual_shipping_cost_base=Decimal("350.00"),
            )
            with pytest.raises(AppError) as blocked:
                await orders.ship_order(session, tid, finance, oid, ship)
            assert blocked.value.code == "ERR_BALANCE_PAYMENT_REQUIRED"
            await payments.record_and_verify_payment(
                session,
                tid,
                finance,
                slip.model_copy(
                    update={
                        "rp_id": balance.id,
                        "amount_received": Decimal("700"),
                        "actual_fx_rate": Decimal("7.18"),
                        "gateway_fee": Decimal("20"),
                    }
                ),
            )
            assert (await orders.get_order(session, tid, oid)).payment_status == PaymentStatus.PAID
            await orders.ship_order(session, tid, finance, oid, ship)

            cvp = await profits.recalculate_order_profit(
                session, tid, oid, channel_msg_cost_base=Decimal("2.50")
            )
            assert cvp.base_currency == "CNY"
            assert cvp.gross_revenue_base == Decimal("7200.00")
            assert cvp.cogs_base == Decimal("4000.00")
            assert cvp.payment_gateway_fee_base == Decimal("252.35")
            assert cvp.shipping_and_duty_base == Decimal("350.00")
            assert cvp.fx_gain_loss_base == Decimal("1.00")
            assert cvp.net_contribution_margin_base == Decimal("2596.15")
            assert cvp.contribution_margin_pct == Decimal("36.06")
            assert (await profits.get_profit_summary(session, tid)).total_orders == 1
        finally:
            await session.rollback()


def test_finance_payment_rejects_binary_float_before_decimal_conversion() -> None:
    from pydantic import ValidationError

    from erp_core.finance.schemas import PaymentCreate

    with pytest.raises(ValidationError, match="decimal string"):
        PaymentCreate.model_validate(
            {
                "rp_id": uuid4(),
                "payment_method": "tt_wire",
                "currency": "USD",
                "amount_received": 0.1,
                "actual_fx_rate": "7.20",
            }
        )
