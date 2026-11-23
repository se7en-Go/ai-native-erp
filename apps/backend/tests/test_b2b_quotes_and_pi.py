from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register the domain model modules so SQLAlchemy can resolve the cross-module
# string foreign keys (mirrors the api_main composition root).
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import MoqTierInput, ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.crm.pdf_renderer import InMemoryPdfStorage, ProformaInvoicePdfRenderer
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import (
    ConvertedOrderSummary,
    ConvertedReceivableSummary,
    ConvertQuoteRequest,
    LeadCreate,
    OpportunityCreate,
    QuoteCreate,
    QuoteItemInput,
    QuoteRepriceRequest,
)
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    FulfillmentStatus,
    Incoterm,
    OrderMode,
    PaymentStatus,
    QuoteStatus,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal


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


class _PostgresConversionBridge:
    """Real PostgreSQL port implementation used in Task 3.3 before Tasks 3.4 & 3.6 land."""

    async def create_order_from_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_mode: OrderMode,
        customer_id: UUID,
        quote_id: UUID,
        warehouse_id: UUID,
        currency: str,
        fx_rate_to_base: Decimal,
        subtotal_amount: Decimal,
        shipping_charged: Decimal,
        tax_collected: Decimal,
        total_amount: Decimal,
        channel_metadata: dict,
        items: list[dict],
    ) -> ConvertedOrderSummary:
        order_id = uuid4()
        order_no = f"ORD-{uuid4().hex[:8].upper()}"
        await session.execute(
            text(
                """
                INSERT INTO orders (
                    id, tenant_id, order_no, order_mode, customer_id, quote_id, currency,
                    fx_rate_to_base, subtotal_amount, shipping_charged, discount_amount,
                    tax_collected, total_amount, fulfillment_status, payment_status,
                    channel_metadata
                ) VALUES (
                    :id, :tid, :ono, :mode, :cid, :qid, :ccy,
                    :fx, :sub, :ship, 0, :tax, :tot, 'pending_payment', 'unpaid',
                    CAST(:meta AS jsonb)
                )
                """
            ),
            {
                "id": order_id,
                "tid": tenant_id,
                "ono": order_no,
                "mode": order_mode.value,
                "cid": customer_id,
                "qid": quote_id,
                "ccy": currency,
                "fx": fx_rate_to_base,
                "sub": subtotal_amount,
                "ship": shipping_charged,
                "tax": tax_collected,
                "tot": total_amount,
                "meta": __import__("json").dumps(channel_metadata),
            },
        )
        for item in sorted(items, key=lambda x: str(x["variant_id"])):
            await session.execute(
                text(
                    """
                    INSERT INTO order_items (
                        id, tenant_id, order_id, variant_id, sku_snapshot,
                        quantity, unit_price, unit_cost_base_snapshot, line_total
                    ) VALUES (
                        :id, :tid, :oid, :vid, :sku, :qty, :up, :uc, :lt
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "oid": order_id,
                    "vid": item["variant_id"],
                    "sku": item["sku_snapshot"],
                    "qty": item["quantity"],
                    "up": item["unit_price"],
                    "uc": item["unit_cost_base_snapshot"],
                    "lt": item["line_total"],
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_ledger (
                        tenant_id, warehouse_id, variant_id, event_type,
                        qty_delta_on_hand, qty_delta_reserved,
                        reference_type, reference_id, idempotency_key
                    ) VALUES (
                        :tid, :wid, :vid, 'reserve_order', 0, :qty, 'order', :oid, :ikey
                    )
                    """
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": item["variant_id"],
                    "qty": item["quantity"],
                    "oid": order_id,
                    "ikey": f"quote-conv:{order_id}:{item['variant_id']}",
                },
            )
            updated = await session.execute(
                text(
                    """
                    UPDATE inventory_balances
                    SET reserved_qty = reserved_qty + :qty
                    WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                      AND (on_hand_qty - reserved_qty) >= :qty
                    RETURNING reserved_qty
                    """
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": item["variant_id"],
                    "qty": item["quantity"],
                },
            )
            if updated.scalar_one_or_none() is None:
                raise AppError(
                    slug="insufficient-stock",
                    status_code=409,
                    code="ERR_INSUFFICIENT_STOCK",
                    title="Insufficient Stock",
                    detail="Not enough stock.",
                )

        return ConvertedOrderSummary(
            id=order_id,
            order_no=order_no,
            order_mode=order_mode,
            fulfillment_status=FulfillmentStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.UNPAID,
            total_amount=total_amount,
            currency=currency,
        )

    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal,
    ) -> list[ConvertedReceivableSummary]:
        dep = (total_amount * deposit_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bal = (total_amount - dep).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out: list[ConvertedReceivableSummary] = []
        for label, amt in [("b2b_30_deposit", dep), ("b2b_70_balance", bal)]:
            rid = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO receivables_payables (
                        id, tenant_id, order_id, direction, milestone_label,
                        due_amount, paid_amount, currency, status
                    ) VALUES (
                        :id, :tid, :oid, 'receivable', :lbl, :amt, 0, :ccy, 'open'
                    )
                    """
                ),
                {
                    "id": rid,
                    "tid": tenant_id,
                    "oid": order_id,
                    "lbl": label,
                    "amt": amt,
                    "ccy": currency,
                },
            )
            out.append(
                ConvertedReceivableSummary(
                    id=rid,
                    milestone_label=label,
                    due_amount=amt,
                    paid_amount=Decimal("0.00"),
                    currency=currency,
                    status="open",
                )
            )
        return out

    async def recalculate_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO order_profit_snapshots (order_id, tenant_id, base_currency)
                VALUES (:oid, :tid, 'CNY')
                ON CONFLICT (order_id) DO NOTHING
                """
            ),
            {"oid": order_id, "tid": tenant_id},
        )


@pytest.mark.asyncio
async def test_quanzhou_led_500pc_fob_quote_pdf_expired_fx_and_convert() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    manager_id = uuid4()
    agent_id = uuid4()
    customer_id = uuid4()
    company_id = uuid4()
    warehouse_id = uuid4()

    manager = CurrentPrincipal(
        user_id=manager_id, tenant_id=tenant_id, role=UserRole.MANAGER, email="mgr@qz.cn"
    )
    agent = CurrentPrincipal(
        user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT, email="agent@qz.cn"
    )

    try:
        # tenants / b2b_companies / customers are FORCE-RLS protected: seed via app_runtime.
        async with tenant_session(rt_factory, tenant_id) as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {
                    "id": tenant_id,
                    "name": "Quanzhou Smart LED Factory",
                    "slug": f"qz-{tenant_id.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO b2b_companies (
                        id, tenant_id, legal_name, country_code, default_incoterm
                    ) VALUES (:id, :tid, 'Iberia Lighting Distributors SL', 'ES', 'FOB')
                    """
                ),
                {"id": company_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO customers (
                        id, tenant_id, customer_type, company_id, display_name,
                        primary_email, preferred_language
                    ) VALUES (
                        :id, :tid, 'b2b_contact', :cid, 'Carlos Mendez',
                        'carlos@iberialight.es', 'es'
                    )
                    """
                ),
                {"id": customer_id, "tid": tenant_id, "cid": company_id},
            )

        catalog_svc = CatalogService()
        storage = InMemoryPdfStorage()
        bridge = _PostgresConversionBridge()
        quote_svc = QuoteService(
            order_port=bridge,
            payment_port=bridge,
            profit_port=bridge,
            pdf_renderer=ProformaInvoicePdfRenderer(storage=storage),
        )

        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session,
                tenant_id,
                actor_id=str(manager_id),
                payload=ProductCreate(
                    spu_code="QZ-LED-STRIP-5M",
                    category="led_lighting",
                    hs_code="9405429000",
                    origin_country="CN",
                    i18n_specs={
                        "en": {"title": "5M IP65 Smart LED Strip 24V"},
                        "es": {"title": "Tira LED Inteligente 5M IP65 24V"},
                    },
                    certifications=["CE", "RoHS"],
                ),
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id=str(manager_id),
                role=UserRole.MANAGER,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("42.0000"),
                    base_currency="CNY",
                    min_margin_floor_pct=Decimal("15.00"),
                    retail_price_usd=Decimal("18.50"),
                    b2b_moq_tiers=[
                        MoqTierInput(min_qty=50, unit_price_usd=Decimal("12.00")),
                        MoqTierInput(min_qty=500, unit_price_usd=Decimal("8.50")),
                    ],
                    weight_grams=300,
                ),
            )
            # Seed 600 units on hand
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (
                        tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty
                    )
                    VALUES (:tid, :wid, :vid, 600, 0)
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

            lead = await quote_svc.create_lead(
                session,
                tenant_id,
                LeadCreate(
                    customer_id=customer_id,
                    source_channel="made_in_china_email",
                    lead_type="b2b_inquiry",
                    qualification_score=88,
                    extracted_requirements={
                        "sku": "LED-STRIP-5M-IP65",
                        "qty": 500,
                        "incoterm": "FOB",
                    },
                ),
            )
            opp = await quote_svc.create_opportunity(
                session,
                tenant_id,
                OpportunityCreate(
                    lead_id=lead.id,
                    company_id=company_id,
                    customer_id=customer_id,
                    title="Iberia Lighting 500pcs LED Strip FOB Xiamen",
                    expected_amount=Decimal("4450.00"),
                    currency="USD",
                    owner_user_id=manager_id,
                ),
            )

            # Agent trying to override below margin floor must be rejected (403)
            with pytest.raises(AppError) as exc_override:
                await quote_svc.create_quote(
                    session,
                    tenant_id,
                    principal=agent,
                    payload=QuoteCreate(
                        opportunity_id=opp.id,
                        is_proforma_invoice=True,
                        incoterm=Incoterm.FOB,
                        port_of_loading="Xiamen, China",
                        port_of_destination="Valencia, Spain",
                        payment_terms="30% T/T deposit, 70% before shipment",
                        base_currency="CNY",
                        quote_currency="USD",
                        fx_rate_snapshot=Decimal("7.200000"),
                        fx_locked_until=datetime.now(UTC) - timedelta(hours=1),
                        freight_fee=Decimal("200.00"),
                        override_margin_floor=True,
                        items=[
                            QuoteItemInput(
                                variant_id=variant.id,
                                quantity=500,
                                unit_price_quote_ccy=Decimal("6.00"),
                            )
                        ],
                    ),
                )
            assert exc_override.value.status_code == 403

            # Create valid v1 quote (expired FX lock for testing rejection)
            v1 = await quote_svc.create_quote(
                session,
                tenant_id,
                principal=manager,
                payload=QuoteCreate(
                    opportunity_id=opp.id,
                    is_proforma_invoice=True,
                    incoterm=Incoterm.FOB,
                    port_of_loading="Xiamen, China",
                    port_of_destination="Valencia, Spain",
                    payment_terms="30% T/T deposit, 70% before shipment",
                    base_currency="CNY",
                    quote_currency="USD",
                    fx_rate_snapshot=Decimal("7.200000"),
                    fx_locked_until=datetime.now(UTC) - timedelta(hours=2),
                    freight_fee=Decimal("200.00"),
                    items=[QuoteItemInput(variant_id=variant.id, quantity=500, language="es")],
                ),
            )
            assert v1.version == 1
            assert v1.total_amount == Decimal("4450.00")
            assert v1.items[0].moq_tier_applied == 500
            assert v1.items[0].unit_price_quote_ccy == Decimal("8.5000")

            # Generate multilingual WeasyPrint PDF
            pdf_result = await quote_svc.generate_pi_pdf(session, tenant_id, v1.id, language="es")
            assert pdf_result.pdf_r2_key.endswith(f"{v1.quote_no}-v1.pdf")
            stored_bytes = storage.objects[pdf_result.pdf_r2_key]
            assert stored_bytes.startswith(b"%PDF-")

            await quote_svc.send_quote(session, tenant_id, manager, v1.id)
            await quote_svc.accept_quote(session, tenant_id, manager, v1.id)

            # Converting expired-FX quote MUST fail
            with pytest.raises(AppError) as exc_fx:
                await quote_svc.convert_quote_to_order(
                    session,
                    tenant_id,
                    principal=manager,
                    quote_id=v1.id,
                    payload=ConvertQuoteRequest(
                        warehouse_id=warehouse_id, order_mode=OrderMode.B2B_WHOLESALE
                    ),
                )
            assert exc_fx.value.code == "ERR_QUOTE_FX_EXPIRED"

            # Reprice creates immutable v2 with fresh FX lock (leaving v1 untouched)
            v2 = await quote_svc.reprice_quote_version(
                session,
                tenant_id,
                principal=manager,
                quote_id=v1.id,
                payload=QuoteRepriceRequest(
                    fx_rate_snapshot=Decimal("7.250000"),
                    fx_locked_until=datetime.now(UTC) + timedelta(days=7),
                    freight_fee=Decimal("250.00"),
                ),
            )
            assert v2.version == 2
            assert v2.quote_no == v1.quote_no
            assert v2.total_amount == Decimal("4500.00")
            v1_check = await quote_svc.get_quote(session, tenant_id, v1.id)
            assert v1_check.total_amount == Decimal("4450.00")

            await quote_svc.send_quote(session, tenant_id, manager, v2.id)
            await quote_svc.accept_quote(session, tenant_id, manager, v2.id)

            # 1-Click convert v2 -> B2B Wholesale Order + 30%/70% Receivables + Reserved Stock
            conv = await quote_svc.convert_quote_to_order(
                session,
                tenant_id,
                principal=manager,
                quote_id=v2.id,
                payload=ConvertQuoteRequest(
                    warehouse_id=warehouse_id, order_mode=OrderMode.B2B_WHOLESALE
                ),
            )
            assert conv.quote.status == QuoteStatus.CONVERTED
            assert conv.order.order_mode == OrderMode.B2B_WHOLESALE
            assert conv.order.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT
            assert conv.order.payment_status == PaymentStatus.UNPAID
            assert conv.order.total_amount == Decimal("4500.00")
            assert len(conv.receivables) == 2
            milestones = {r.milestone_label: r.due_amount for r in conv.receivables}
            assert milestones["b2b_30_deposit"] == Decimal("1350.00")
            assert milestones["b2b_70_balance"] == Decimal("3150.00")

            bal_row = (
                await session.execute(
                    text(
                        "SELECT on_hand_qty, reserved_qty FROM inventory_balances "
                        "WHERE tenant_id = :tid AND warehouse_id = :wid "
                        "AND variant_id = :vid"
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                )
            ).one()
            assert bal_row.on_hand_qty == 600
            assert bal_row.reserved_qty == 500
    finally:
        # `inventory_ledger` is append-only: DELETE is revoked from app_runtime by design,
        # so its rows (and therefore products/variants/tenant) are intentionally retained.
        async with tenant_session(rt_factory, tenant_id) as conn:
            for stmt in (
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM payment_records WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM quote_items WHERE tenant_id = :tid",
                "DELETE FROM quotes WHERE tenant_id = :tid",
                "DELETE FROM opportunities WHERE tenant_id = :tid",
                "DELETE FROM leads WHERE tenant_id = :tid",
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
                "DELETE FROM b2b_companies WHERE tenant_id = :tid",
            ):
                await conn.execute(text(stmt), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()


@pytest.mark.asyncio
async def test_b2b_30_70_milestones_settlement_and_cvp_margin_recompute() -> None:
    """Exercise the finances behind the CRM conversion path on a real PostgreSQL cluster.

    `QuoteService` reaches `PaymentService` / `OrderProfitService` through its runtime
    adapter, so those Decimal-only paths are verified here rather than left unexercised.
    """
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    variant_id = uuid4()
    deposit_rp_id = uuid4()

    try:
        async with tenant_session(rt_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, base_currency) "
                    "VALUES (:id, :name, :slug, 'CNY')"
                ),
                {"id": tenant_id, "name": "Milestone Co", "slug": f"ms-{tenant_id.hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:id, :tid, 'b2b_company_rep', 'Iberia Buyer')"
                ),
                {"id": customer_id, "tid": tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO orders ("
                    " id, tenant_id, order_no, order_mode, customer_id, currency,"
                    " fx_rate_to_base, subtotal_amount, shipping_charged, total_amount,"
                    " fulfillment_status, payment_status"
                    ") VALUES ("
                    " :oid, :tid, 'ORD-MS-001', 'b2b_wholesale', :cid, 'USD',"
                    " 7.200000, 4320.00, 30.00, 4500.00, 'confirmed_reserved', 'unpaid'"
                    ")"
                ),
                {"oid": order_id, "tid": tenant_id, "cid": customer_id},
            )
            product = await CatalogService().create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-MS-LED", category="led_lighting"),
            )
            variant = await CatalogService().create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.MANAGER,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("30.0000"),
                    retail_price_usd=Decimal("19.99"),
                    weight_grams=300,
                ),
            )
            variant_id = variant.id
            await session.execute(
                text(
                    "INSERT INTO order_items ("
                    " id, tenant_id, order_id, variant_id, sku_snapshot, quantity,"
                    " unit_price, unit_cost_base_snapshot, line_total"
                    ") VALUES ("
                    " :iid, :tid, :oid, :vid, 'LED-STRIP-5M-IP65', 500, 8.6400, 30.0000, 4320.00"
                    ")"
                ),
                {"iid": uuid4(), "tid": tenant_id, "oid": order_id, "vid": variant_id},
            )

            payment_svc = PaymentService()
            receivables = await payment_svc.create_b2b_milestone_receivables(
                session,
                tenant_id=tenant_id,
                order_id=order_id,
                total_amount=Decimal("4500.00"),
                currency="usd",
                deposit_ratio=Decimal("0.30"),
            )
            by_label = {r.milestone_label: r for r in receivables}
            assert set(by_label) == {"b2b_30_deposit", "b2b_70_balance"}
            assert by_label["b2b_30_deposit"].due_amount == Decimal("1350.00")
            assert by_label["b2b_70_balance"].due_amount == Decimal("3150.00")
            assert sum(r.due_amount for r in receivables) == Decimal("4500.00")
            assert all(r.currency == "USD" for r in receivables)
            deposit_rp_id = by_label["b2b_30_deposit"].id

            # Deposit settles -> order becomes deposit_paid
            await payment_svc.record_payment(
                session,
                tenant_id=tenant_id,
                rp_id=deposit_rp_id,
                amount_received=Decimal("1350.00"),
                currency="USD",
                actual_fx_rate=Decimal("7.150000"),
                payment_method="bank_transfer",
                gateway_fee=Decimal("12.50"),
            )
            assert (
                await payment_svc.sync_order_payment_status(session, tenant_id, order_id)
                == PaymentStatus.DEPOSIT_PAID
            )

            # Balance settles -> order fully paid
            await payment_svc.record_payment(
                session,
                tenant_id=tenant_id,
                rp_id=by_label["b2b_70_balance"].id,
                amount_received=Decimal("3150.00"),
                currency="USD",
                actual_fx_rate=Decimal("7.260000"),
            )
            assert (
                await payment_svc.sync_order_payment_status(session, tenant_id, order_id)
                == PaymentStatus.PAID
            )

            # CVP: gross 4500*7.2 = 32400.00, cogs 500*30.00 = 15000.00,
            # shipping/duty 30*7.2 = 216.00 -> margin 17184.00 (all Decimal)
            snapshot = await OrderProfitService().recalculate_order_profit(
                session, tenant_id=tenant_id, order_id=order_id
            )
            assert snapshot.gross_revenue_base == Decimal("32400.00")
            assert snapshot.cogs_base == Decimal("15000.00")
            assert snapshot.shipping_and_duty_base == Decimal("216.00")
            assert snapshot.net_contribution_margin_base == Decimal("17184.00")
    finally:
        async with tenant_session(rt_factory, tenant_id) as conn:
            for stmt in (
                "DELETE FROM payment_records WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
            ):
                await conn.execute(text(stmt), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
