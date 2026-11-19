from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import PlatformSkuMapping, Product, ProductVariant
from erp_core.catalog.schemas import (
    LiveSkuCheckRead,
    MoqTierInput,
    ProductCreate,
    ProductRead,
    ResolvedChannelSku,
    SkuMappingCreate,
    SkuMappingRead,
    SkuMappingUpdate,
    VariantCreate,
    VariantMappingsBundleRead,
    VariantMappingWithLabelRead,
    VariantPricingInternal,
    VariantRead,
    VariantUpdate,
)
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event


class CatalogService:
    """Product, variant, MOQ tier pricing, and multi-platform SKU bundle resolution."""

    @staticmethod
    def serialize_variant_for_role(variant: ProductVariant, role: UserRole) -> VariantRead:
        """Mask cost_price_base and min_margin_floor_pct for agent role."""
        tiers = [
            MoqTierInput(
                min_qty=int(t["min_qty"]),
                unit_price_usd=Decimal(str(t["unit_price_usd"])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
            )
            for t in (variant.b2b_moq_tiers or [])
        ]
        hide_costs = role == UserRole.AGENT
        return VariantRead(
            id=variant.id,
            tenant_id=variant.tenant_id,
            product_id=variant.product_id,
            sku=variant.sku,
            attributes=variant.attributes,
            cost_price_base=None if hide_costs else Decimal(str(variant.cost_price_base)),
            base_currency=variant.base_currency,
            min_margin_floor_pct=None if hide_costs else Decimal(str(variant.min_margin_floor_pct)),
            retail_price_usd=Decimal(str(variant.retail_price_usd)),
            b2b_moq_tiers=tiers,
            weight_grams=variant.weight_grams,
            carton_dims_cm=variant.carton_dims_cm,
            lead_time_days=variant.lead_time_days,
            created_at=variant.created_at,
        )

    async def create_product(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: ProductCreate,
    ) -> ProductRead:
        product = Product(
            tenant_id=tenant_id,
            spu_code=payload.spu_code.strip(),
            category=payload.category.strip(),
            hs_code=payload.hs_code.strip() if payload.hs_code else None,
            origin_country=payload.origin_country.upper(),
            i18n_specs=payload.i18n_specs,
            certifications=payload.certifications,
            ai_faq_facts=payload.ai_faq_facts,
        )
        session.add(product)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-spu",
                status_code=409,
                code="ERR_DUPLICATE_SPU",
                title="Duplicate SPU",
                detail=f"Product with spu_code '{payload.spu_code}' already exists.",
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.product.created",
            resource_type="product",
            resource_id=product.id,
            after_state={"spu_code": product.spu_code, "category": product.category},
        )
        return ProductRead.model_validate(product)

    async def list_products(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        limit: int = 20,
        offset: int = 0,
        search: str | None = None,
    ) -> PaginatedResponse[ProductRead]:
        stmt = select(Product).where(Product.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Product).where(Product.tenant_id == tenant_id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(Product.spu_code.ilike(pattern) | Product.category.ilike(pattern))
            count_stmt = count_stmt.where(
                Product.spu_code.ilike(pattern) | Product.category.ilike(pattern)
            )

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(Product.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [ProductRead.model_validate(r) for r in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def create_variant(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: VariantCreate,
        role: UserRole,
    ) -> VariantRead:
        product = (
            await session.execute(
                select(Product).where(
                    Product.tenant_id == tenant_id, Product.id == payload.product_id
                )
            )
        ).scalar_one_or_none()
        if product is None:
            raise AppError(
                slug="product-not-found",
                status_code=404,
                code="ERR_PRODUCT_NOT_FOUND",
                title="Product Not Found",
                detail=f"Product '{payload.product_id}' does not exist.",
            )

        serialized_tiers = [
            {
                "min_qty": tier.min_qty,
                "unit_price_usd": str(
                    tier.unit_price_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                ),
            }
            for tier in sorted(payload.b2b_moq_tiers, key=lambda t: t.min_qty)
        ]
        variant = ProductVariant(
            tenant_id=tenant_id,
            product_id=payload.product_id,
            sku=payload.sku.strip(),
            attributes=payload.attributes,
            cost_price_base=payload.cost_price_base.quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            ),
            base_currency=payload.base_currency.upper(),
            min_margin_floor_pct=payload.min_margin_floor_pct.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            retail_price_usd=payload.retail_price_usd.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            b2b_moq_tiers=serialized_tiers,
            weight_grams=payload.weight_grams,
            carton_dims_cm=payload.carton_dims_cm,
            lead_time_days=payload.lead_time_days,
        )
        session.add(variant)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-sku",
                status_code=409,
                code="ERR_DUPLICATE_SKU",
                title="Duplicate SKU",
                detail=f"Variant with sku '{payload.sku}' already exists.",
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.variant.created",
            resource_type="product_variant",
            resource_id=variant.id,
            after_state={"sku": variant.sku, "retail_price_usd": str(variant.retail_price_usd)},
        )
        return self.serialize_variant_for_role(variant, role)

    async def list_variants(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        role: UserRole,
        product_id: UUID | None = None,
        sku: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[VariantRead]:
        stmt = select(ProductVariant).where(ProductVariant.tenant_id == tenant_id)
        count_stmt = (
            select(func.count())
            .select_from(ProductVariant)
            .where(ProductVariant.tenant_id == tenant_id)
        )
        if product_id is not None:
            stmt = stmt.where(ProductVariant.product_id == product_id)
            count_stmt = count_stmt.where(ProductVariant.product_id == product_id)
        if sku is not None:
            stmt = stmt.where(ProductVariant.sku == sku.strip())
            count_stmt = count_stmt.where(ProductVariant.sku == sku.strip())
        if q is not None and q.strip():
            pat = f"%{q.strip()}%"
            stmt = stmt.where(ProductVariant.sku.ilike(pat))
            count_stmt = count_stmt.where(ProductVariant.sku.ilike(pat))

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(ProductVariant.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [self.serialize_variant_for_role(row, role) for row in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def get_variant_for_role(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
        role: UserRole,
    ) -> VariantRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        return self.serialize_variant_for_role(variant, role)

    async def get_variant_pricing_internal(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
    ) -> VariantPricingInternal:
        row = (
            await session.execute(
                select(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id)
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        variant, product = row
        return VariantPricingInternal(
            id=variant.id,
            product_id=variant.product_id,
            sku=variant.sku,
            cost_price_base=Decimal(str(variant.cost_price_base)),
            base_currency=variant.base_currency,
            min_margin_floor_pct=Decimal(str(variant.min_margin_floor_pct)),
            retail_price_usd=Decimal(str(variant.retail_price_usd)),
            b2b_moq_tiers=list(variant.b2b_moq_tiers or []),
            i18n_specs=dict(product.i18n_specs or {}),
            hs_code=product.hs_code,
        )

    async def resolve_moq_unit_price(
        self,
        retail_price_usd: Decimal,
        b2b_moq_tiers: list[dict[str, Any]],
        qty: int,
    ) -> tuple[Decimal, int | None]:
        """Select highest matching MOQ tier unit price using Decimal; fall back to retail price."""
        sorted_tiers = sorted(b2b_moq_tiers, key=lambda item: int(item["min_qty"]), reverse=True)
        for tier in sorted_tiers:
            min_qty = int(tier["min_qty"])
            if qty >= min_qty:
                price = Decimal(str(tier["unit_price_usd"])).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                return price, min_qty
        return Decimal(str(retail_price_usd)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        ), None

    async def create_sku_mapping(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: SkuMappingCreate,
    ) -> SkuMappingRead:
        mapping = PlatformSkuMapping(
            tenant_id=tenant_id,
            variant_id=payload.variant_id,
            channel_account_id=payload.channel_account_id,
            external_product_id=payload.external_product_id.strip(),
            external_sku=payload.external_sku.strip(),
            bundle_multiplier=payload.bundle_multiplier,
            sync_inventory_enabled=payload.sync_inventory_enabled,
        )
        session.add(mapping)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-sku-mapping",
                status_code=409,
                code="ERR_DUPLICATE_SKU_MAPPING",
                title="Duplicate SKU Mapping",
                detail=(
                    f"External SKU '{payload.external_sku}' is already mapped for "
                    f"channel account '{payload.channel_account_id}'."
                ),
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sku_mapping.created",
            resource_type="platform_sku_mapping",
            resource_id=mapping.id,
            after_state={
                "external_sku": mapping.external_sku,
                "variant_id": str(mapping.variant_id),
                "bundle_multiplier": mapping.bundle_multiplier,
            },
        )
        return SkuMappingRead.model_validate(mapping)

    async def list_sku_mappings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[SkuMappingRead]:
        stmt = select(PlatformSkuMapping).where(PlatformSkuMapping.tenant_id == tenant_id)
        count_stmt = (
            select(func.count())
            .select_from(PlatformSkuMapping)
            .where(PlatformSkuMapping.tenant_id == tenant_id)
        )
        if channel_account_id is not None:
            stmt = stmt.where(PlatformSkuMapping.channel_account_id == channel_account_id)
            count_stmt = count_stmt.where(
                PlatformSkuMapping.channel_account_id == channel_account_id
            )

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            (
                await session.execute(
                    stmt.order_by(PlatformSkuMapping.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        items = [SkuMappingRead.model_validate(r) for r in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def resolve_sku_for_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        external_sku: str,
        external_qty: int,
    ) -> ResolvedChannelSku | None:
        if external_qty <= 0:
            raise ValueError("external_qty must be positive")
        cleaned_sku = external_sku.strip()
        row = (
            await session.execute(
                select(PlatformSkuMapping, ProductVariant)
                .join(ProductVariant, ProductVariant.id == PlatformSkuMapping.variant_id)
                .where(
                    PlatformSkuMapping.tenant_id == tenant_id,
                    PlatformSkuMapping.channel_account_id == channel_account_id,
                    PlatformSkuMapping.external_sku == cleaned_sku,
                )
            )
        ).one_or_none()

        if row is not None:
            mapping, variant = row
            return ResolvedChannelSku(
                variant_id=variant.id,
                internal_sku=variant.sku,
                bundle_multiplier=mapping.bundle_multiplier,
                deduct_qty=external_qty * mapping.bundle_multiplier,
                cost_price_base=Decimal(str(variant.cost_price_base)),
                base_currency=variant.base_currency,
            )

        # Direct fallback if the external SKU matches the internal SKU verbatim
        direct_variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == tenant_id, ProductVariant.sku == cleaned_sku
                )
            )
        ).scalar_one_or_none()
        if direct_variant is None:
            return None
        return ResolvedChannelSku(
            variant_id=direct_variant.id,
            internal_sku=direct_variant.sku,
            bundle_multiplier=1,
            deduct_qty=external_qty,
            cost_price_base=Decimal(str(direct_variant.cost_price_base)),
            base_currency=direct_variant.base_currency,
        )

    async def update_variant(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        variant_id: UUID,
        payload: VariantUpdate,
        role: UserRole,
    ) -> VariantRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        if payload.retail_price_usd is not None:
            variant.retail_price_usd = payload.retail_price_usd.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if payload.b2b_moq_tiers is not None:
            variant.b2b_moq_tiers = [
                {"min_qty": t.min_qty, "unit_price_usd": str(t.unit_price_usd)}
                for t in sorted(payload.b2b_moq_tiers, key=lambda x: x.min_qty)
            ]
        if payload.min_margin_floor_pct is not None:
            variant.min_margin_floor_pct = payload.min_margin_floor_pct.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if payload.lead_time_days is not None:
            variant.lead_time_days = payload.lead_time_days
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.variant.updated",
            resource_type="product_variant",
            resource_id=variant.id,
            after_state={"sku": variant.sku, "retail_price_usd": str(variant.retail_price_usd)},
        )
        return self.serialize_variant_for_role(variant, role)

    async def list_variant_mappings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
        role: UserRole,
    ) -> VariantMappingsBundleRead:
        variant_read = await self.get_variant_for_role(session, tenant_id, variant_id, role)
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id, m.tenant_id, m.variant_id, m.channel_account_id,
                           ca.display_name AS channel_label, m.external_product_id,
                           m.external_sku, m.bundle_multiplier,
                           m.sync_inventory_enabled, m.created_at
                    FROM platform_sku_mappings m
                    LEFT JOIN channel_accounts ca
                        ON ca.id = m.channel_account_id AND ca.tenant_id = m.tenant_id
                    WHERE m.tenant_id = :tid AND m.variant_id = :vid
                    ORDER BY m.created_at DESC
                    """
                ),
                {"tid": tenant_id, "vid": variant_id},
            )
        ).all()
        mappings = [
            VariantMappingWithLabelRead(
                id=r.id,
                tenant_id=r.tenant_id,
                variant_id=r.variant_id,
                channel_account_id=r.channel_account_id,
                channel_label=r.channel_label,
                external_product_id=r.external_product_id,
                external_sku=r.external_sku,
                bundle_multiplier=int(r.bundle_multiplier),
                sync_inventory_enabled=bool(r.sync_inventory_enabled),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return VariantMappingsBundleRead(variant=variant_read, mappings=mappings)

    async def update_sku_mapping(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        mapping_id: UUID,
        payload: SkuMappingUpdate,
    ) -> SkuMappingRead:
        mapping = (
            await session.execute(
                select(PlatformSkuMapping).where(
                    PlatformSkuMapping.tenant_id == tenant_id, PlatformSkuMapping.id == mapping_id
                )
            )
        ).scalar_one_or_none()
        if mapping is None:
            raise AppError(
                slug="sku-mapping-not-found",
                status_code=404,
                code="ERR_SKU_MAPPING_NOT_FOUND",
                title="SKU Mapping Not Found",
                detail=f"SKU mapping '{mapping_id}' not found.",
            )
        if payload.external_sku is not None:
            mapping.external_sku = payload.external_sku.strip()
        if payload.bundle_multiplier is not None:
            mapping.bundle_multiplier = payload.bundle_multiplier
        if payload.sync_inventory_enabled is not None:
            mapping.sync_inventory_enabled = payload.sync_inventory_enabled
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sku_mapping.updated",
            resource_type="platform_sku_mapping",
            resource_id=mapping.id,
            after_state={
                "external_sku": mapping.external_sku,
                "bundle_multiplier": mapping.bundle_multiplier,
                "sync_inventory_enabled": mapping.sync_inventory_enabled,
            },
        )
        return SkuMappingRead.model_validate(mapping)

    async def live_check_sku(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        sku: str,
        qty: int = 1,
        incoterm: str = "FOB",
    ) -> LiveSkuCheckRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == tenant_id,
                    func.lower(ProductVariant.sku) == sku.strip().lower(),
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"SKU '{sku}' not found.",
            )
        avail_raw = (
            await session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(on_hand_qty - reserved_qty), 0)
                    FROM inventory_balances
                    WHERE tenant_id = :tid AND variant_id = :vid
                    """
                ),
                {"tid": tenant_id, "vid": variant.id},
            )
        ).scalar_one()
        unit_price, moq_tier = await self.resolve_moq_unit_price(
            Decimal(str(variant.retail_price_usd)), list(variant.b2b_moq_tiers or []), qty
        )
        return LiveSkuCheckRead(
            sku=variant.sku,
            available_qty=int(avail_raw),
            unit_price_quote_ccy=unit_price,
            moq_tier_applied=moq_tier,
            incoterm=incoterm.upper(),
            source_tool_call_id=f"sql-sku-{uuid4().hex[:8]}",
        )
