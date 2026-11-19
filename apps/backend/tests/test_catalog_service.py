from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from erp_schema_bootstrap import ensure_erp_schema, register_plan02_metadata_stand_ins  # noqa: I001
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.api_main import app
from erp_core.catalog.router import router as catalog_router
from erp_core.catalog.schemas import (
    MoqTierInput,
    ProductCreate,
    SkuMappingCreate,
    VariantCreate,
)
from erp_core.catalog.service import CatalogService
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.security import create_access_token


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


def _make_jwt(user_id, tenant_id, role: UserRole, email: str) -> str:
    return create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=email,
        settings=get_settings(),
    )


@pytest.mark.asyncio
async def test_catalog_moq_tiers_bundle_multiplier_and_agent_cost_masking() -> None:
    await ensure_erp_schema(_migrator_url())
    register_plan02_metadata_stand_ins()
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    admin_id = uuid4()
    agent_id = uuid4()
    channel_account_id = uuid4()

    try:
        # tenants, tenant_memberships and channel_accounts are FORCE-RLS protected, so the
        # fixture must seed through the same fail-closed runtime path production uses.
        async with tenant_session(rt_factory, tenant_id) as conn:
            slug = f"cat-{tenant_id.hex[:8]}"
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Quanzhou LED Co", "slug": slug},
            )
            for uid, email, role in [
                (admin_id, f"admin-{tenant_id.hex[:6]}@qz.cn", UserRole.ADMIN.value),
                (agent_id, f"agent-{tenant_id.hex[:6]}@qz.cn", UserRole.AGENT.value),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, full_name) "
                        "VALUES (:id, :email, 'hash', :name)"
                    ),
                    {"id": uid, "email": email, "name": role},
                )
                await conn.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                        "VALUES (:id, :tid, :uid, :role)"
                    ),
                    {"id": uuid4(), "tid": tenant_id, "uid": uid, "role": role},
                )
            await conn.execute(
                text(
                    """
                    INSERT INTO channel_accounts (
                        id, tenant_id, channel_type, external_account_id,
                        display_name, status, rate_limit_profile
                    ) VALUES (
                        :id, :tid, 'shopify', 'qz-led.myshopify.com',
                        'QZ Shopify', 'active', '{}'::jsonb
                    )
                    """
                ),
                {"id": channel_account_id, "tid": tenant_id},
            )

        svc = CatalogService()
        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await svc.create_product(
                session,
                tenant_id,
                actor_id=str(admin_id),
                payload=ProductCreate(
                    spu_code="QZ-STRIP-5M",
                    category="smart_lighting",
                    hs_code="9405429000",
                    origin_country="CN",
                    i18n_specs={
                        "en": {
                            "title": "5M IP65 Waterproof RGBW LED Strip",
                            "selling_points": ["IP65 Silicon Tube", "CRI>90"],
                            "materials": "FPCB + Silicone",
                        },
                        "es": {
                            "title": "Tira LED RGBW 5M IP65 Impermeable",
                            "selling_points": ["Silicona IP65"],
                        },
                    },
                    certifications=["CE", "FCC", "RoHS"],
                    ai_faq_facts={"warranty_months": 24, "voltage": "24V DC"},
                ),
            )
            variant = await svc.create_variant(
                session,
                tenant_id,
                actor_id=str(admin_id),
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    attributes={"color": "RGBW", "plug": "EU"},
                    cost_price_base=Decimal("32.5000"),
                    base_currency="CNY",
                    min_margin_floor_pct=Decimal("18.00"),
                    retail_price_usd=Decimal("19.90"),
                    b2b_moq_tiers=[
                        MoqTierInput(min_qty=50, unit_price_usd=Decimal("12.50")),
                        MoqTierInput(min_qty=500, unit_price_usd=Decimal("8.90")),
                    ],
                    weight_grams=310,
                    carton_dims_cm={"l": 42, "w": 32, "h": 28, "pcs_per_carton": 40},
                    lead_time_days=10,
                ),
            )
            assert variant.cost_price_base == Decimal("32.5000")
            assert variant.min_margin_floor_pct == Decimal("18.00")

            # Verify MOQ tier selection with pure Decimal
            p_sample, tier_sample = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=10,
            )
            assert p_sample == Decimal("19.90")
            assert tier_sample is None

            p_50, tier_50 = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=200,
            )
            assert p_50 == Decimal("12.50")
            assert tier_50 == 50

            p_500, tier_500 = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=500,
            )
            assert p_500 == Decimal("8.90")
            assert tier_500 == 500

            # Create 2-pack bundle SKU mapping
            await svc.create_sku_mapping(
                session,
                tenant_id,
                actor_id=str(admin_id),
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=channel_account_id,
                    external_product_id="gid://shopify/Product/99001",
                    external_sku="SHOP-LED-5M-2PACK",
                    bundle_multiplier=2,
                    sync_inventory_enabled=True,
                ),
            )
            resolved = await svc.resolve_sku_for_channel(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                external_sku="SHOP-LED-5M-2PACK",
                external_qty=3,
            )
            assert resolved is not None
            assert resolved.internal_sku == "LED-STRIP-5M-IP65"
            assert resolved.bundle_multiplier == 2
            assert resolved.deduct_qty == 6

        # HTTP RBAC check: agent MUST NOT receive cost_price_base or min_margin_floor_pct.
        # The catalog router is mounted here because `erp_core.api_main` is owned by the
        # shared wiring and must stay untouched until Plan 02 lands (see the PR's mount-point
        # note); the endpoint, DI, RBAC dependency and RLS path under test are all real.
        if not getattr(app.state, "_plan03_catalog_mounted", False):
            app.include_router(catalog_router, prefix="/api/v1")
            app.state._plan03_catalog_mounted = True
        app.state.session_factory = rt_factory
        app.state.settings = get_settings()
        agent_token = _make_jwt(
            agent_id, tenant_id, UserRole.AGENT, f"agent-{tenant_id.hex[:6]}@qz.cn"
        )
        admin_token = _make_jwt(
            admin_id, tenant_id, UserRole.ADMIN, f"admin-{tenant_id.hex[:6]}@qz.cn"
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            agent_resp = await client.get(
                "/api/v1/catalog/variants",
                headers={"Authorization": f"Bearer {agent_token}"},
            )
            assert agent_resp.status_code == 200
            agent_item = agent_resp.json()["items"][0]
            assert agent_item["sku"] == "LED-STRIP-5M-IP65"
            assert agent_item["cost_price_base"] is None
            assert agent_item["min_margin_floor_pct"] is None

            admin_resp = await client.get(
                "/api/v1/catalog/variants",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert admin_resp.status_code == 200
            admin_item = admin_resp.json()["items"][0]
            assert Decimal(str(admin_item["cost_price_base"])) == Decimal("32.5000")
            assert Decimal(str(admin_item["min_margin_floor_pct"])) == Decimal("18.00")
    finally:
        async with tenant_session(rt_factory, tenant_id) as conn:
            await conn.execute(
                text("DELETE FROM platform_sku_mappings WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM tenant_memberships WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM users WHERE id = ANY(:uids)"), {"uids": [admin_id, agent_id]}
            )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
