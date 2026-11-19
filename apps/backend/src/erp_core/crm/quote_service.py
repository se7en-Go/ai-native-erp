from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.crm.models import Lead, Opportunity, Quote, QuoteItem
from erp_core.crm.pdf_renderer import PiLineRenderItem, PiRenderContext, ProformaInvoicePdfRenderer
from erp_core.crm.schemas import (
    ConvertedOrderSummary,
    ConvertedReceivableSummary,
    ConvertQuoteRequest,
    ConvertQuoteResponse,
    LeadCreate,
    LeadRead,
    OpportunityCreate,
    OpportunityRead,
    QuoteCreate,
    QuoteItemInput,
    QuoteItemRead,
    QuotePdfResponse,
    QuoteRead,
    QuoteRepriceRequest,
)
from erp_core.shared.enums import (
    FulfillmentStatus,
    LeadStatus,
    OpportunityStage,
    OrderMode,
    QuoteStatus,
    UserRole,
)
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class QuoteOrderPort(Protocol):
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
        channel_metadata: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> ConvertedOrderSummary: ...


class QuotePaymentPort(Protocol):
    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal,
    ) -> list[ConvertedReceivableSummary]: ...


class QuoteProfitPort(Protocol):
    async def recalculate_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> Any: ...


class _RuntimeConversionAdapter:
    """Lazy runtime bridge to Tasks 3.4 OrderService and 3.6 Finance services."""

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
        channel_metadata: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> ConvertedOrderSummary:
        from erp_core.orders.order_service import OrderService
        from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal

        order = await OrderService().create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            payload=OrderCreateInternal(
                order_mode=order_mode,
                customer_id=customer_id,
                quote_id=quote_id,
                warehouse_id=warehouse_id,
                currency=currency,
                fx_rate_to_base=fx_rate_to_base,
                subtotal_amount=subtotal_amount,
                shipping_charged=shipping_charged,
                discount_amount=Decimal("0.00"),
                tax_collected=tax_collected,
                total_amount=total_amount,
                initial_fulfillment_status=FulfillmentStatus.PENDING_PAYMENT,
                channel_metadata=channel_metadata,
                items=[OrderItemInternal(**item) for item in items],
            ),
        )
        return ConvertedOrderSummary(
            id=order.id,
            order_no=order.order_no,
            order_mode=order.order_mode,
            fulfillment_status=order.fulfillment_status,
            payment_status=order.payment_status,
            total_amount=order.total_amount,
            currency=order.currency,
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
        from erp_core.finance.payment_service import PaymentService

        rows = await PaymentService().create_b2b_milestone_receivables(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            total_amount=total_amount,
            currency=currency,
            deposit_ratio=deposit_ratio,
        )
        return [
            ConvertedReceivableSummary(
                id=r.id,
                milestone_label=r.milestone_label,
                due_amount=r.due_amount,
                paid_amount=r.paid_amount,
                currency=r.currency,
                status=r.status,
            )
            for r in rows
        ]

    async def recalculate_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> Any:
        from erp_core.finance.profit_service import OrderProfitService

        return await OrderProfitService().recalculate_order_profit(
            session, tenant_id=tenant_id, order_id=order_id
        )


class QuoteService:
    """B2B leads, opportunities, immutable quotes/PIs, PDF export, 1-click conversion."""

    def __init__(
        self,
        catalog_service: CatalogService | None = None,
        order_port: QuoteOrderPort | None = None,
        payment_port: QuotePaymentPort | None = None,
        profit_port: QuoteProfitPort | None = None,
        pdf_renderer: ProformaInvoicePdfRenderer | None = None,
    ) -> None:
        self.catalog = catalog_service or CatalogService()
        runtime_adapter = _RuntimeConversionAdapter()
        self.order_port: QuoteOrderPort = order_port or runtime_adapter
        self.payment_port: QuotePaymentPort = payment_port or runtime_adapter
        self.profit_port: QuoteProfitPort = profit_port or runtime_adapter
        self.pdf_renderer = pdf_renderer or ProformaInvoicePdfRenderer()

    async def create_lead(
        self, session: AsyncSession, tenant_id: UUID, payload: LeadCreate
    ) -> LeadRead:
        lead = Lead(
            tenant_id=tenant_id,
            customer_id=payload.customer_id,
            source_channel=payload.source_channel,
            utm_campaign=payload.utm_campaign,
            lead_type=payload.lead_type,
            qualification_score=payload.qualification_score,
            extracted_requirements=payload.extracted_requirements,
            status=LeadStatus.NEW,
        )
        session.add(lead)
        await session.flush()
        return LeadRead.model_validate(lead)

    async def list_leads(
        self, session: AsyncSession, tenant_id: UUID, limit: int = 20, offset: int = 0
    ) -> PaginatedResponse[LeadRead]:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
        rows = (
            (
                await session.execute(
                    select(Lead)
                    .where(Lead.tenant_id == tenant_id)
                    .order_by(Lead.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [LeadRead.model_validate(r) for r in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def create_opportunity(
        self, session: AsyncSession, tenant_id: UUID, payload: OpportunityCreate
    ) -> OpportunityRead:
        opp = Opportunity(
            tenant_id=tenant_id,
            lead_id=payload.lead_id,
            company_id=payload.company_id,
            customer_id=payload.customer_id,
            title=payload.title.strip(),
            stage=payload.stage,
            expected_amount=payload.expected_amount.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            currency=payload.currency.upper(),
            expected_close_date=payload.expected_close_date,
            owner_user_id=payload.owner_user_id,
        )
        session.add(opp)
        await session.flush()
        return OpportunityRead.model_validate(opp)

    async def list_opportunities(
        self, session: AsyncSession, tenant_id: UUID, limit: int = 20, offset: int = 0
    ) -> PaginatedResponse[OpportunityRead]:
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Opportunity)
                    .where(Opportunity.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
        rows = (
            (
                await session.execute(
                    select(Opportunity)
                    .where(Opportunity.tenant_id == tenant_id)
                    .order_by(Opportunity.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [OpportunityRead.model_validate(r) for r in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def update_opportunity_stage(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        opportunity_id: UUID,
        stage: OpportunityStage,
    ) -> OpportunityRead:
        opp = (
            await session.execute(
                select(Opportunity).where(
                    Opportunity.tenant_id == tenant_id, Opportunity.id == opportunity_id
                )
            )
        ).scalar_one_or_none()
        if opp is None:
            raise AppError(
                slug="opportunity-not-found",
                status_code=404,
                code="ERR_OPPORTUNITY_NOT_FOUND",
                title="Opportunity Not Found",
                detail=f"Opportunity '{opportunity_id}' not found.",
            )
        prev_stage = opp.stage.value
        opp.stage = stage
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="b2b.opportunity.stage_updated",
            resource_type="opportunity",
            resource_id=opp.id,
            before_state={"stage": prev_stage},
            after_state={"stage": stage.value},
        )
        return OpportunityRead.model_validate(opp)

    async def _build_quote_items_and_check_margin(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        item_inputs: list[QuoteItemInput],
        fx_rate_snapshot: Decimal,
        override_margin_floor: bool,
    ) -> tuple[list[QuoteItem], Decimal, bool]:
        if override_margin_floor and principal.role == UserRole.AGENT:
            raise AppError(
                slug="margin-floor-override-forbidden",
                status_code=403,
                code="ERR_MARGIN_FLOOR_OVERRIDE_FORBIDDEN",
                title="Margin Override Forbidden",
                detail="Agent role cannot override variant min_margin_floor_pct.",
            )

        built_items: list[QuoteItem] = []
        subtotal_quote_ccy = Decimal("0.00")
        margin_override_used = False

        for item_in in item_inputs:
            vp = await self.catalog.get_variant_pricing_internal(
                session, tenant_id, item_in.variant_id
            )
            if item_in.unit_price_quote_ccy is not None:
                unit_price = item_in.unit_price_quote_ccy.quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                moq_tier = None
            else:
                unit_price, moq_tier = await self.catalog.resolve_moq_unit_price(
                    vp.retail_price_usd, vp.b2b_moq_tiers, item_in.quantity
                )

            unit_price_base = (unit_price * fx_rate_snapshot).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if unit_price_base <= Decimal("0"):
                margin_pct = Decimal("-100.00")
            else:
                margin_pct = (
                    (unit_price_base - vp.cost_price_base) / unit_price_base * Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if margin_pct < vp.min_margin_floor_pct:
                if not override_margin_floor:
                    raise AppError(
                        slug="margin-floor-violation",
                        status_code=422,
                        code="ERR_MARGIN_FLOOR_VIOLATION",
                        title="Margin Floor Violation",
                        detail=(
                            f"SKU '{vp.sku}' margin {margin_pct}% is below floor "
                            f"{vp.min_margin_floor_pct}%."
                        ),
                    )
                margin_override_used = True

            line_total = (Decimal(item_in.quantity) * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            subtotal_quote_ccy += line_total

            lang_specs = vp.i18n_specs.get(item_in.language) or vp.i18n_specs.get("en") or {}
            description = item_in.description_override or lang_specs.get("title") or vp.sku

            built_items.append(
                QuoteItem(
                    tenant_id=tenant_id,
                    variant_id=vp.id,
                    sku_snapshot=vp.sku,
                    description_i18n=str(description),
                    quantity=item_in.quantity,
                    moq_tier_applied=moq_tier,
                    unit_price_quote_ccy=unit_price,
                    unit_cost_base_snapshot=vp.cost_price_base,
                    line_total_quote_ccy=line_total,
                )
            )

        return built_items, subtotal_quote_ccy, margin_override_used

    async def _serialize_quote(
        self, session: AsyncSession, tenant_id: UUID, quote: Quote
    ) -> QuoteRead:
        rows = (
            (
                await session.execute(
                    select(QuoteItem).where(
                        QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return QuoteRead(
            id=quote.id,
            tenant_id=quote.tenant_id,
            opportunity_id=quote.opportunity_id,
            quote_no=quote.quote_no,
            version=quote.version,
            is_proforma_invoice=quote.is_proforma_invoice,
            incoterm=quote.incoterm,
            port_of_loading=quote.port_of_loading,
            port_of_destination=quote.port_of_destination,
            payment_terms=quote.payment_terms,
            base_currency=quote.base_currency,
            quote_currency=quote.quote_currency,
            fx_rate_snapshot=Decimal(str(quote.fx_rate_snapshot)),
            fx_locked_until=quote.fx_locked_until,
            freight_fee=Decimal(str(quote.freight_fee)),
            insurance_fee=Decimal(str(quote.insurance_fee)),
            customs_duty_estimate=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            pdf_r2_key=quote.pdf_r2_key,
            status=quote.status,
            converted_order_id=quote.converted_order_id,
            created_at=quote.created_at,
            items=[QuoteItemRead.model_validate(item) for item in rows],
        )

    async def get_quote(self, session: AsyncSession, tenant_id: UUID, quote_id: UUID) -> QuoteRead:
        quote = (
            await session.execute(
                select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
            )
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        return await self._serialize_quote(session, tenant_id, quote)

    async def list_quotes(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        opportunity_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[QuoteRead]:
        stmt = select(Quote).where(Quote.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Quote).where(Quote.tenant_id == tenant_id)
        if opportunity_id is not None:
            stmt = stmt.where(Quote.opportunity_id == opportunity_id)
            count_stmt = count_stmt.where(Quote.opportunity_id == opportunity_id)
        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(Quote.created_at.desc(), Quote.version.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [await self._serialize_quote(session, tenant_id, q) for q in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def create_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: QuoteCreate,
    ) -> QuoteRead:
        opp = (
            await session.execute(
                select(Opportunity).where(
                    Opportunity.tenant_id == tenant_id, Opportunity.id == payload.opportunity_id
                )
            )
        ).scalar_one_or_none()
        if opp is None:
            raise AppError(
                slug="opportunity-not-found",
                status_code=404,
                code="ERR_OPPORTUNITY_NOT_FOUND",
                title="Opportunity Not Found",
                detail=f"Opportunity '{payload.opportunity_id}' not found.",
            )

        (
            built_items,
            subtotal,
            margin_override_used,
        ) = await self._build_quote_items_and_check_margin(
            session,
            tenant_id,
            principal,
            payload.items,
            payload.fx_rate_snapshot,
            payload.override_margin_floor,
        )
        freight = payload.freight_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        insurance = payload.insurance_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        duty = payload.customs_duty_estimate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_amount = (subtotal + freight + insurance + duty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        now = datetime.now(UTC)
        quote_no = f"PI-{now:%Y%m}-{uuid4().hex[:6].upper()}"
        quote = Quote(
            tenant_id=tenant_id,
            opportunity_id=opp.id,
            quote_no=quote_no,
            version=1,
            is_proforma_invoice=payload.is_proforma_invoice,
            incoterm=payload.incoterm,
            port_of_loading=payload.port_of_loading,
            port_of_destination=payload.port_of_destination,
            payment_terms=payload.payment_terms,
            base_currency=payload.base_currency.upper(),
            quote_currency=payload.quote_currency.upper(),
            fx_rate_snapshot=payload.fx_rate_snapshot.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            fx_locked_until=payload.fx_locked_until,
            freight_fee=freight,
            insurance_fee=insurance,
            customs_duty_estimate=duty,
            total_amount=total_amount,
            status=QuoteStatus.DRAFT,
        )
        session.add(quote)
        await session.flush()

        for qi in built_items:
            qi.quote_id = quote.id
            session.add(qi)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.margin_override" if margin_override_used else "quote.created",
            resource_type="quote",
            resource_id=quote.id,
            after_state={
                "quote_no": quote.quote_no,
                "version": quote.version,
                "total_amount": str(quote.total_amount),
            },
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def reprice_quote_version(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        quote_id: UUID,
        payload: QuoteRepriceRequest,
    ) -> QuoteRead:
        """Create a new immutable version (max_version + 1); historical versions stay intact."""
        prev = (
            await session.execute(
                select(Quote)
                .where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if prev is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        if prev.status == QuoteStatus.CONVERTED:
            raise AppError(
                slug="quote-already-converted",
                status_code=409,
                code="ERR_QUOTE_ALREADY_CONVERTED",
                title="Quote Already Converted",
                detail="Cannot reprice a quote that has already been converted to an order.",
            )

        max_ver = int(
            (
                await session.execute(
                    select(func.max(Quote.version)).where(
                        Quote.tenant_id == tenant_id, Quote.quote_no == prev.quote_no
                    )
                )
            ).scalar_one()
        )
        if payload.items is not None:
            item_inputs = payload.items
        else:
            prev_items = (
                (
                    await session.execute(
                        select(QuoteItem).where(
                            QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == prev.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            item_inputs = [
                QuoteItemInput(
                    variant_id=pi.variant_id,
                    quantity=pi.quantity,
                    unit_price_quote_ccy=Decimal(str(pi.unit_price_quote_ccy)),
                    description_override=pi.description_i18n,
                )
                for pi in prev_items
            ]

        (
            built_items,
            subtotal,
            margin_override_used,
        ) = await self._build_quote_items_and_check_margin(
            session,
            tenant_id,
            principal,
            item_inputs,
            payload.fx_rate_snapshot,
            payload.override_margin_floor,
        )
        freight = (
            payload.freight_fee
            if payload.freight_fee is not None
            else Decimal(str(prev.freight_fee))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        insurance = (
            payload.insurance_fee
            if payload.insurance_fee is not None
            else Decimal(str(prev.insurance_fee))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        duty = (
            payload.customs_duty_estimate
            if payload.customs_duty_estimate is not None
            else Decimal(str(prev.customs_duty_estimate))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_amount = (subtotal + freight + insurance + duty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        new_quote = Quote(
            tenant_id=tenant_id,
            opportunity_id=prev.opportunity_id,
            quote_no=prev.quote_no,
            version=max_ver + 1,
            is_proforma_invoice=prev.is_proforma_invoice,
            incoterm=prev.incoterm,
            port_of_loading=prev.port_of_loading,
            port_of_destination=prev.port_of_destination,
            payment_terms=payload.payment_terms or prev.payment_terms,
            base_currency=prev.base_currency,
            quote_currency=prev.quote_currency,
            fx_rate_snapshot=payload.fx_rate_snapshot.quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            ),
            fx_locked_until=payload.fx_locked_until,
            freight_fee=freight,
            insurance_fee=insurance,
            customs_duty_estimate=duty,
            total_amount=total_amount,
            status=QuoteStatus.DRAFT,
        )
        session.add(new_quote)
        await session.flush()

        for qi in built_items:
            qi.quote_id = new_quote.id
            session.add(qi)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.repriced_version",
            resource_type="quote",
            resource_id=new_quote.id,
            before_state={"previous_quote_id": str(prev.id), "previous_version": prev.version},
            after_state={"version": new_quote.version, "total_amount": str(new_quote.total_amount)},
        )
        return await self._serialize_quote(session, tenant_id, new_quote)

    async def send_quote(
        self, session: AsyncSession, tenant_id: UUID, principal: CurrentPrincipal, quote_id: UUID
    ) -> QuoteRead:
        quote = (
            await session.execute(
                select(Quote)
                .where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        if quote.status not in {QuoteStatus.DRAFT, QuoteStatus.PENDING_APPROVAL}:
            raise AppError(
                slug="invalid-quote-state",
                status_code=409,
                code="ERR_INVALID_QUOTE_STATE",
                title="Invalid Quote State",
                detail=f"Cannot send quote in status '{quote.status}'.",
            )
        if quote.status == QuoteStatus.PENDING_APPROVAL and principal.role == UserRole.AGENT:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Approval Required",
                detail="Pending approval quote requires manager/admin/owner.",
            )
        quote.status = QuoteStatus.SENT
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.sent",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"status": quote.status.value},
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def accept_quote(
        self, session: AsyncSession, tenant_id: UUID, principal: CurrentPrincipal, quote_id: UUID
    ) -> QuoteRead:
        quote = (
            await session.execute(
                select(Quote)
                .where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        if quote.status != QuoteStatus.SENT:
            raise AppError(
                slug="invalid-quote-state",
                status_code=409,
                code="ERR_INVALID_QUOTE_STATE",
                title="Invalid Quote State",
                detail=f"Quote must be 'sent' before acceptance (found '{quote.status}').",
            )
        quote.status = QuoteStatus.ACCEPTED
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.accepted",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"status": quote.status.value},
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def generate_pi_pdf(
        self, session: AsyncSession, tenant_id: UUID, quote_id: UUID, language: str = "en"
    ) -> QuotePdfResponse:
        quote = (
            await session.execute(
                select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
            )
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        items = (
            (
                await session.execute(
                    select(QuoteItem).where(
                        QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id
                    )
                )
            )
            .scalars()
            .all()
        )

        info_row = (
            await session.execute(
                text(
                    """
                    SELECT
                        t.name AS seller_name,
                        c.display_name AS buyer_contact,
                        c.primary_email AS buyer_email,
                        COALESCE(bc.legal_name, c.display_name) AS buyer_company
                    FROM opportunities o
                    JOIN tenants t ON t.id = o.tenant_id
                    JOIN customers c ON c.id = o.customer_id
                    LEFT JOIN b2b_companies bc ON bc.id = o.company_id
                    WHERE o.tenant_id = :tid AND o.id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": quote.opportunity_id},
            )
        ).one()

        ctx = PiRenderContext(
            tenant_id=tenant_id,
            seller_name=info_row.seller_name,
            buyer_company=info_row.buyer_company,
            buyer_contact=info_row.buyer_contact,
            buyer_email=info_row.buyer_email,
            quote_no=quote.quote_no,
            version=quote.version,
            is_proforma_invoice=quote.is_proforma_invoice,
            incoterm=quote.incoterm.value,
            port_of_loading=quote.port_of_loading or "",
            port_of_destination=quote.port_of_destination or "",
            payment_terms=quote.payment_terms,
            quote_currency=quote.quote_currency,
            fx_rate_snapshot=Decimal(str(quote.fx_rate_snapshot)),
            fx_locked_until=quote.fx_locked_until,
            issued_at=quote.created_at,
            freight_fee=Decimal(str(quote.freight_fee)),
            insurance_fee=Decimal(str(quote.insurance_fee)),
            customs_duty_estimate=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            items=[
                PiLineRenderItem(
                    sku=qi.sku_snapshot,
                    description=qi.description_i18n,
                    quantity=qi.quantity,
                    unit_price=Decimal(str(qi.unit_price_quote_ccy)),
                    line_total=Decimal(str(qi.line_total_quote_ccy)),
                )
                for qi in items
            ],
            language=language,
        )
        key, url = await self.pdf_renderer.render_and_upload(ctx)
        quote.pdf_r2_key = key
        await session.flush()
        return QuotePdfResponse(
            quote_id=quote.id,
            quote_no=quote.quote_no,
            version=quote.version,
            pdf_r2_key=key,
            download_url=url,
        )

    async def convert_quote_to_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        quote_id: UUID,
        payload: ConvertQuoteRequest,
    ) -> ConvertQuoteResponse:
        quote = (
            await session.execute(
                select(Quote)
                .where(Quote.tenant_id == tenant_id, Quote.id == quote_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(
                slug="quote-not-found",
                status_code=404,
                code="ERR_QUOTE_NOT_FOUND",
                title="Quote Not Found",
                detail=f"Quote '{quote_id}' not found.",
            )
        if quote.status == QuoteStatus.CONVERTED:
            raise AppError(
                slug="quote-already-converted",
                status_code=409,
                code="ERR_QUOTE_ALREADY_CONVERTED",
                title="Already Converted",
                detail="Quote was already converted to an order.",
            )
        if quote.status != QuoteStatus.ACCEPTED:
            raise AppError(
                slug="quote-not-accepted",
                status_code=422,
                code="ERR_QUOTE_NOT_ACCEPTED",
                title="Quote Not Accepted",
                detail="Quote must be in 'accepted' status before 1-click conversion.",
            )

        now = datetime.now(UTC)
        if now > quote.fx_locked_until:
            raise AppError(
                slug="quote-fx-expired",
                status_code=422,
                code="ERR_QUOTE_FX_EXPIRED",
                title="Quote FX Lock Expired",
                detail=(
                    f"Quote FX lock expired at {quote.fx_locked_until.isoformat()}; "
                    "create a repriced quote version."
                ),
            )

        opp = (
            await session.execute(
                select(Opportunity).where(
                    Opportunity.tenant_id == tenant_id, Opportunity.id == quote.opportunity_id
                )
            )
        ).scalar_one()
        q_items = (
            (
                await session.execute(
                    select(QuoteItem).where(
                        QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id
                    )
                )
            )
            .scalars()
            .all()
        )

        subtotal = sum(
            (Decimal(str(i.line_total_quote_ccy)) for i in q_items), Decimal("0.00")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        shipping_charged = (
            Decimal(str(quote.freight_fee)) + Decimal(str(quote.insurance_fee))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        order = await self.order_port.create_order_from_quote(
            session,
            tenant_id=tenant_id,
            actor_id=str(principal.user_id),
            order_mode=payload.order_mode,
            customer_id=opp.customer_id,
            quote_id=quote.id,
            warehouse_id=payload.warehouse_id,
            currency=quote.quote_currency,
            fx_rate_to_base=Decimal(str(quote.fx_rate_snapshot)),
            subtotal_amount=subtotal,
            shipping_charged=shipping_charged,
            tax_collected=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            channel_metadata={
                "incoterm": quote.incoterm.value,
                "quote_no": quote.quote_no,
                "quote_version": quote.version,
            },
            items=[
                {
                    "variant_id": qi.variant_id,
                    "sku_snapshot": qi.sku_snapshot,
                    "quantity": qi.quantity,
                    "unit_price": Decimal(str(qi.unit_price_quote_ccy)),
                    "unit_cost_base_snapshot": Decimal(str(qi.unit_cost_base_snapshot)),
                    "line_total": Decimal(str(qi.line_total_quote_ccy)),
                }
                for qi in q_items
            ],
        )

        receivables = await self.payment_port.create_b2b_milestone_receivables(
            session,
            tenant_id=tenant_id,
            order_id=order.id,
            total_amount=Decimal(str(order.total_amount)),
            currency=order.currency,
            deposit_ratio=payload.deposit_ratio,
        )
        await self.profit_port.recalculate_order_profit(
            session, tenant_id=tenant_id, order_id=order.id
        )

        quote.status = QuoteStatus.CONVERTED
        quote.converted_order_id = order.id
        opp.stage = OpportunityStage.CLOSED_WON
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.converted_to_order",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"order_id": str(order.id), "order_no": order.order_no},
        )
        quote_read = await self._serialize_quote(session, tenant_id, quote)
        return ConvertQuoteResponse(quote=quote_read, order=order, receivables=receivables)
