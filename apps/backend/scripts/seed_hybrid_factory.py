from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from uuid import NAMESPACE_DNS, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import every ORM module so SQLAlchemy can resolve the cross-module foreign keys
# (purchase_orders -> tenants/users, ai_creative_assets -> users) before the first flush.
# The API composition root does the same at runtime; a standalone CLI script must not skip it.
import erp_core.ai_engine.models  # noqa: F401
import erp_core.billing.models  # noqa: F401
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.supply_chain.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.ingestion import DocumentIngestionService
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.channels.ecommerce.base import CredentialVaultHelper, register_channel_routing
from erp_core.shared.config import Settings, get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session
from erp_core.shared.enums import Incoterm, OnboardingStage, UserRole
from erp_core.supply_chain import procurement
from erp_core.supply_chain.schemas import ReceivePurchaseOrder
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.security import hash_password

SEED_DEMO_PASSWORD = "FactoryPass2026!"

TENANT_A_ID = uuid5(NAMESPACE_DNS, "quanzhou-led-factory.erp.local")
TENANT_B_ID = uuid5(NAMESPACE_DNS, "shenzhen-audio-dtc.erp.local")


@dataclass(frozen=True)
class SeedSummary:
    tenant_a_id: UUID
    tenant_a_slug: str
    tenant_b_id: UUID
    tenant_b_slug: str


async def _upsert_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    slug: str,
    name: str,
    preset: str,
    currency: str,
    timezone: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO tenants (id, slug, name, business_preset, base_currency, timezone) "
            "VALUES (:id, :slug, :name, :preset, :currency, :tz) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  name = EXCLUDED.name, "
            "  business_preset = EXCLUDED.business_preset, "
            "  base_currency = EXCLUDED.base_currency, "
            "  timezone = EXCLUDED.timezone, "
            "  is_active = true, "
            "  shredded_at = NULL, "
            "  updated_at = now()"
        ),
        {
            "id": str(tenant_id),
            "slug": slug,
            "name": name,
            "preset": preset,
            "currency": currency,
            "tz": timezone,
        },
    )


async def _upsert_user_and_membership(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    full_name: str,
    role: UserRole,
    password_hash: str,
) -> UUID:
    user_id = uuid5(NAMESPACE_DNS, f"user:{email}")
    saved_user_id = await session.scalar(
        text(
            "INSERT INTO users (id, email, full_name, password_hash, is_active) "
            "VALUES (:id, :email, :fn, :ph, true) "
            "ON CONFLICT (email) DO UPDATE SET "
            "  full_name = EXCLUDED.full_name, "
            "  password_hash = EXCLUDED.password_hash, "
            "  is_active = true, "
            "  updated_at = now() "
            "RETURNING id"
        ),
        {
            "id": str(user_id),
            "email": email,
            "fn": full_name,
            "ph": password_hash,
        },
    )
    resolved_user_id = UUID(str(saved_user_id))
    membership_id = uuid5(NAMESPACE_DNS, f"membership:{tenant_id}:{resolved_user_id}")
    await session.execute(
        text(
            "INSERT INTO tenant_memberships (id, tenant_id, user_id, role, is_active) "
            "VALUES (:mid, :tid, :uid, :role, true) "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
            "  role = EXCLUDED.role, "
            "  is_active = true"
        ),
        {
            "mid": str(membership_id),
            "tid": str(tenant_id),
            "uid": str(resolved_user_id),
            "role": role.value,
        },
    )
    return resolved_user_id


async def _upsert_encrypted_credential(
    session: AsyncSession,
    crypto: EnvelopeCryptoService,
    *,
    tenant_id: UUID,
    account_id: UUID,
    cred_type: str,
    plaintext_secret: str,
) -> None:
    ciphertext, nonce, auth_tag, key_version = await crypto.encrypt_secret(
        session, tenant_id, account_id, cred_type, plaintext_secret
    )
    cred_id = uuid5(NAMESPACE_DNS, f"cred:{tenant_id}:{account_id}:{cred_type}")
    await session.execute(
        text(
            "INSERT INTO channel_credentials "
            "(id, tenant_id, channel_account_id, credential_type, ciphertext, nonce, "
            "auth_tag, key_version) "
            "VALUES (:id, :tid, :aid, :ctype, :ct, :nonce, :tag, :kv) "
            "ON CONFLICT (tenant_id, channel_account_id, credential_type) DO UPDATE SET "
            "  ciphertext = EXCLUDED.ciphertext, "
            "  nonce = EXCLUDED.nonce, "
            "  auth_tag = EXCLUDED.auth_tag, "
            "  key_version = EXCLUDED.key_version, "
            "  updated_at = now()"
        ),
        {
            "id": str(cred_id),
            "tid": str(tenant_id),
            "aid": str(account_id),
            "ctype": cred_type,
            "ct": ciphertext,
            "nonce": nonce,
            "tag": auth_tag,
            "kv": key_version,
        },
    )


async def seed_hybrid_factory(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    demo_data: bool = False,
) -> SeedSummary:
    """Seed the two demo tenants.

    Accounts, credentials and onboarding state are always created (idempotent).
    `demo_data=True` additionally populates the flagship workspace with the demo
    catalogue, channel accounts, inbox conversation and AI proposal used by the
    Docker `demo-seed` profile; it is skipped when the workspace already has
    products, so it stays safe to re-run.
    """
    crypto = EnvelopeCryptoService(settings)
    password_hash = hash_password(SEED_DEMO_PASSWORD)

    async with tenant_session(session_factory, TENANT_A_ID) as session:
        await _upsert_tenant(
            session,
            tenant_id=TENANT_A_ID,
            slug="quanzhou-led-factory",
            name="Quanzhou Smart LED Co., Ltd.",
            preset="hybrid_factory_b2b_b2c",
            currency="USD",
            timezone="Asia/Shanghai",
        )
        await crypto.provision_tenant_dek(session, TENANT_A_ID)

        tenant_a_users = [
            ("owner@quanzhou-led.example.com", "Jerry Lin", UserRole.OWNER),
            ("admin@quanzhou-led.example.com", "Alice Chen", UserRole.ADMIN),
            ("manager@quanzhou-led.example.com", "Bob Wu", UserRole.MANAGER),
            ("agent@quanzhou-led.example.com", "Carol Huang", UserRole.AGENT),
            ("finance@quanzhou-led.example.com", "David Zheng", UserRole.FINANCE),
        ]
        owner_uid = TENANT_A_ID
        for email, full_name, role in tenant_a_users:
            uid = await _upsert_user_and_membership(
                session,
                tenant_id=TENANT_A_ID,
                email=email,
                full_name=full_name,
                role=role,
                password_hash=password_hash,
            )
            if role == UserRole.OWNER:
                owner_uid = uid

        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-wa-coexistence"),
            cred_type="wa_oauth_token",
            plaintext_secret="EAA_DEMO_QUANZHOU_WHATSAPP_COEXISTENCE_TOKEN",
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-shopify"),
            cred_type="shopify_oauth_token",
            plaintext_secret="shpat_demo_quanzhou_led_shopify_token",
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-email"),
            cred_type="resend_api_key",
            plaintext_secret="re_demo_quanzhou_led_resend_key",
        )

        completed = [
            OnboardingStage.WORKSPACE_CREATED.value,
            OnboardingStage.CHANNEL_CONNECTING.value,
            OnboardingStage.CHANNEL_VERIFIED.value,
            OnboardingStage.CATALOG_SYNCING.value,
            OnboardingStage.AI_SANDBOX_READY.value,
            OnboardingStage.LIVE_PRODUCTION.value,
        ]
        await session.execute(
            text(
                "INSERT INTO onboarding_states "
                "(tenant_id, current_stage, preset_type, completed_steps, metadata_json) "
                "VALUES (:tid, :stage, 'hybrid_factory_b2b_b2c', "
                "CAST(:steps AS jsonb), CAST(:meta AS jsonb)) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "  current_stage = EXCLUDED.current_stage, "
                "  preset_type = EXCLUDED.preset_type, "
                "  completed_steps = EXCLUDED.completed_steps, "
                "  metadata_json = EXCLUDED.metadata_json, "
                "  updated_at = now()"
            ),
            {
                "tid": str(TENANT_A_ID),
                "stage": OnboardingStage.LIVE_PRODUCTION.value,
                "steps": json.dumps(completed),
                "meta": json.dumps({"seeded": True, "factory_city": "Quanzhou"}),
            },
        )
        await record_audit_event(
            session,
            TENANT_A_ID,
            "system_cron",
            str(owner_uid),
            "seed.hybrid_factory_applied",
            "tenant",
            TENANT_A_ID,
            after_state={"slug": "quanzhou-led-factory", "roles": 5},
        )

        if demo_data and not await session.scalar(
            text("SELECT 1 FROM products WHERE tenant_id = :tid LIMIT 1"),
            {"tid": str(TENANT_A_ID)},
        ):
            await _seed_workspace_demo_data(
                session,
                crypto=crypto,
                tenant_id=TENANT_A_ID,
                slug="quanzhou-led-factory",
                onboarding=False,
                # The workspace is already at LiveProduction from the bootstrap above.
                write_onboarding_state=False,
                order_no="DEMO-SHIPPED",
                tracking_number="DHL-QUANZHOU-LED-FACTORY",
            )
        if demo_data:
            # The Phase 3B roster is reconciled on every demo seed run, so it does not
            # depend on the catalogue guard above (which only seeds once).
            await _seed_phase3b_workforce(
                session, tenant_id=TENANT_A_ID, owner_id=owner_uid
            )

    async with tenant_session(session_factory, TENANT_B_ID) as session:
        await _upsert_tenant(
            session,
            tenant_id=TENANT_B_ID,
            slug="shenzhen-audio-dtc",
            name="Shenzhen Audio DTC Ltd.",
            preset="dtc_brand",
            currency="USD",
            timezone="Asia/Shanghai",
        )
        await crypto.provision_tenant_dek(session, TENANT_B_ID)
        await _upsert_user_and_membership(
            session,
            tenant_id=TENANT_B_ID,
            email="owner@shenzhen-audio.example.com",
            full_name="Kevin Zhou",
            role=UserRole.OWNER,
            password_hash=password_hash,
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_B_ID,
            account_id=uuid5(NAMESPACE_DNS, "shenzhen-shopify"),
            cred_type="shopify_oauth_token",
            plaintext_secret="shpat_demo_shenzhen_audio_shopify_token",
        )
        await session.execute(
            text(
                "INSERT INTO onboarding_states "
                "(tenant_id, current_stage, preset_type, completed_steps, metadata_json) "
                "VALUES (:tid, :stage, 'dtc_brand', CAST(:steps AS jsonb), CAST(:meta AS jsonb)) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "  current_stage = EXCLUDED.current_stage, "
                "  preset_type = EXCLUDED.preset_type, "
                "  completed_steps = EXCLUDED.completed_steps, "
                "  updated_at = now()"
            ),
            {
                "tid": str(TENANT_B_ID),
                "stage": OnboardingStage.CHANNEL_VERIFIED.value,
                "steps": json.dumps(
                    [
                        OnboardingStage.WORKSPACE_CREATED.value,
                        OnboardingStage.CHANNEL_CONNECTING.value,
                        OnboardingStage.CHANNEL_VERIFIED.value,
                    ]
                ),
                "meta": json.dumps({"seeded": True}),
            },
        )

    return SeedSummary(
        tenant_a_id=TENANT_A_ID,
        tenant_a_slug="quanzhou-led-factory",
        tenant_b_id=TENANT_B_ID,
        tenant_b_slug="shenzhen-audio-dtc",
    )


# Phase 3B: the Quanzhou smart-LED factory's digital employee roster. Every row maps onto
# real AiCapability values (and therefore onto ErpToolRegistry tools); the autonomy level
# never exceeds the capability ceiling defined in erp_core.ai_engine.capabilities.
_PHASE3B_EMPLOYEES = (
    {
        "code": "b2b-sales",
        "display_name": "小光",
        "role_title": "B2B 外贸业务员",
        "department": "商务中心",
        "avatar_emoji": "💡",
        "description": "识别 B2B 询盘需求，整理 RFQ 明细并起草报价草案，价格一律取实时 ERP 数据。",
        "capability_codes": ["sales_quoting", "cs_support"],
        "autonomy_level": "L1_proposal",
        "tool_count": 6,
        "system_persona": "严谨的泉州 LED 工厂外贸业务员，只引用 ERP 实时价格与库存。",
    },
    {
        "code": "b2b-order-desk",
        "display_name": "小单",
        "role_title": "B端跟单专员",
        "department": "履约中心",
        "avatar_emoji": "📋",
        "description": "跟进 B 端订单进度、生产交期与样品确认，异常自动升级给人工。",
        "capability_codes": ["sales_quoting", "order_logistics"],
        "autonomy_level": "L1_proposal",
        "tool_count": 5,
        "system_persona": "对订单节点极其敏感的跟单专员，从不承诺未确认的交期。",
    },
    {
        "code": "logistics-forwarder",
        "display_name": "小海",
        "role_title": "物流出船专员",
        "department": "履约中心",
        "avatar_emoji": "🚢",
        "description": "查询 17TRACK 实时节点、整理出船文件，货件停滞 72 小时自动预警。",
        "capability_codes": ["order_logistics"],
        "autonomy_level": "L2_auto",
        "tool_count": 4,
        "system_persona": "只转述物流承运商真实节点的出船专员。",
    },
    {
        "code": "growth-scout",
        "display_name": "小拓",
        "role_title": "获客专员",
        "department": "增长中心",
        "avatar_emoji": "🧭",
        "description": "整理合规线索池、清洗重复买家并生成首触达话术，禁止越权抓取。",
        "capability_codes": ["sales_quoting", "cs_support"],
        "autonomy_level": "L1_proposal",
        "tool_count": 4,
        "system_persona": "严格遵守各平台 ToS 与退订合规的获客专员。",
    },
    {
        "code": "b2c-cs",
        "display_name": "小暖",
        "role_title": "B2C 贴心客服",
        "department": "客户成功",
        "avatar_emoji": "🌷",
        "description": "回答零售买家售前售后问题，引用知识库与订单事实，退款一律转人工。",
        "capability_codes": ["cs_support"],
        "autonomy_level": "L2_auto",
        "tool_count": 5,
        "system_persona": "耐心、简洁的 B2C 客服，绝不自行承诺退款或补发。",
    },
    {
        "code": "finance-assistant",
        "display_name": "小账",
        "role_title": "财务助理",
        "department": "财务中心",
        "avatar_emoji": "📒",
        "description": "按金额匹配应收与银行水单候选，核销动作永远由人工确认。",
        "capability_codes": ["finance_recon"],
        "autonomy_level": "L0_copilot",
        "tool_count": 3,
        "system_persona": "只做匹配建议的财务助理，不执行任何收付核销。",
    },
    {
        "code": "supply-planner",
        "display_name": "小供",
        "role_title": "供应链专员",
        "department": "供应链中心",
        "avatar_emoji": "📦",
        "description": "跟踪安全库存、采购在途与入库进度，输出补货建议。",
        "capability_codes": ["biz_analytics", "order_logistics"],
        "autonomy_level": "L1_proposal",
        "tool_count": 4,
        "system_persona": "只看只读报表的供应链专员，补货决策由人工批准。",
    },
    {
        "code": "biz-analyst",
        "display_name": "小析",
        "role_title": "经营分析师",
        "department": "经营分析",
        "avatar_emoji": "📈",
        "description": "基于只读 ERP 报表输出毛利、渠道结构与库存周转分析。",
        "capability_codes": ["biz_analytics"],
        "autonomy_level": "L2_auto",
        "tool_count": 3,
        "system_persona": "只用批准过的只读 SQL 报表做经营分析的分析师。",
    },
    {
        "code": "content-studio",
        "display_name": "小文",
        "role_title": "文案与社媒专员",
        "department": "品牌营销",
        "avatar_emoji": "✍️",
        "description": "为真实 Catalog SKU 生成多语言商品文案、Listing 与社媒配文草稿。",
        "capability_codes": ["content_studio"],
        "autonomy_level": "L1_proposal",
        "tool_count": 3,
        "system_persona": "不编造参数与认证的多语言文案专员。",
    },
)

_PHASE3B_QUICK_REPLIES = (
    {
        "shortcut_code": "moq-zh",
        "title": "MOQ 起订量回复",
        "category": "报价",
        "language": "zh",
        "body_text": (
            "您好，该型号起订量为 100 条起，500 条起可享阶梯价，具体单价以实时报价单为准。"
        ),
    },
    {
        "shortcut_code": "moq-en",
        "title": "MOQ reply",
        "category": "quoting",
        "language": "en",
        "body_text": (
            "Thank you for your inquiry. Our MOQ starts at 100 units, with tiered "
            "pricing from 500 units. The exact unit price follows the live quotation."
        ),
    },
    {
        "shortcut_code": "lead-time-zh",
        "title": "交期回复",
        "category": "履约",
        "language": "zh",
        "body_text": "常规交期为收到定金后 15-20 天，海运拼箱另计约 25-35 天，以实际排产确认为准。",
    },
    {
        "shortcut_code": "lead-time-en",
        "title": "Lead time reply",
        "category": "fulfillment",
        "language": "en",
        "body_text": (
            "Standard lead time is 15-20 days after deposit; LCL sea freight adds "
            "roughly 25-35 days. Final scheduling is confirmed by our order desk."
        ),
    },
    {
        "shortcut_code": "factory-visit-zh",
        "title": "验厂邀请",
        "category": "信任",
        "language": "zh",
        "body_text": "欢迎到泉州工厂实地验厂，我们可安排产线参观、样品测试与第三方验货对接。",
    },
)

_PHASE3B_ASSETS = (
    (
        "product_copy",
        "LED-COB-24V-5M 德语商品文案",
        "de",
        "LED-COB-24V-5M",
        "为德国经销商撰写 5 米 COB 灯带的商品文案，突出可裁剪分段与无光斑特性。",
        (
            "Flexibles COB-LED-Band, 24 V, 5 m Rolle. Gleichmäßiges Licht ohne sichtbare "
            "Punkte, teilbar an den markierten Segmenten. Ab Lager Quanzhou lieferbar; "
            "technische Daten entnehmen Sie bitte dem aktuellen Datenblatt."
        ),
        "draft",
    ),
    (
        "listing",
        "Amazon Listing bullets",
        "en",
        "LED-COB-24V-5M",
        "Write five marketplace bullet points for the COB strip.",
        (
            "- Seamless COB light line: no visible dots, no dark spots\n"
            "- 5 m roll, cuttable at marked segments for clean installation\n"
            "- 24 V DC operation for stable brightness across the whole run\n"
            "- Suitable for aluminum profiles, cove and cabinet lighting\n"
            "- Factory-direct supply from Quanzhou with traceable batch codes"
        ),
        "approved",
    ),
)


async def _seed_phase3b_workforce(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    owner_id: UUID,
) -> None:
    """Idempotently seed the Phase 3B roster, quick replies and studio assets."""
    for employee in _PHASE3B_EMPLOYEES:
        await session.execute(
            text("""
                INSERT INTO ai_employees (tenant_id, code, display_name, role_title, department,
                    avatar_emoji, description, capability_codes, autonomy_level, is_enabled,
                    tool_count, system_persona)
                VALUES (:tid, :code, :display_name, :role_title, :department, :avatar_emoji,
                    :description, CAST(:capability_codes AS jsonb), :autonomy_level, true,
                    :tool_count, :system_persona)
                ON CONFLICT (tenant_id, code) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    role_title = EXCLUDED.role_title,
                    department = EXCLUDED.department,
                    avatar_emoji = EXCLUDED.avatar_emoji,
                    description = EXCLUDED.description,
                    capability_codes = EXCLUDED.capability_codes,
                    autonomy_level = EXCLUDED.autonomy_level,
                    tool_count = EXCLUDED.tool_count,
                    system_persona = EXCLUDED.system_persona,
                    updated_at = now()
            """),
            {
                **{key: value for key, value in employee.items() if key != "capability_codes"},
                "tid": tenant_id,
                "capability_codes": json.dumps(employee["capability_codes"]),
            },
        )
    for reply in _PHASE3B_QUICK_REPLIES:
        await session.execute(
            text("""
                INSERT INTO quick_reply_templates (tenant_id, title, category, language,
                    body_text, shortcut_code)
                VALUES (:tid, :title, :category, :language, :body_text, :shortcut_code)
                ON CONFLICT (tenant_id, shortcut_code) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    language = EXCLUDED.language,
                    body_text = EXCLUDED.body_text
            """),
            {**reply, "tid": tenant_id},
        )
    for asset_type, title, language, sku_ref, prompt, content, status in _PHASE3B_ASSETS:
        await session.execute(
            text("""
                INSERT INTO ai_creative_assets (id, tenant_id, asset_type, title, language,
                    sku_ref, prompt, content_text, status, created_by)
                VALUES (:id, :tid, :asset_type, :title, :language, :sku_ref, :prompt,
                    :content_text, :status, :created_by)
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    title = EXCLUDED.title,
                    language = EXCLUDED.language,
                    sku_ref = EXCLUDED.sku_ref,
                    prompt = EXCLUDED.prompt,
                    content_text = EXCLUDED.content_text,
                    status = EXCLUDED.status,
                    updated_at = now()
            """),
            {
                "id": uuid5(tenant_id, f"asset:{asset_type}:{sku_ref}"),
                "tid": tenant_id,
                "asset_type": asset_type,
                "title": title,
                "language": language,
                "sku_ref": sku_ref,
                "prompt": prompt,
                "content_text": content,
                "status": status,
                "created_by": owner_id,
            },
        )


async def _seed_phase3a_demo(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    warehouse_id: UUID,
    variant_id: UUID,
    buyer_id: UUID,
    owner_id: UUID,
) -> None:
    """Add the Phase 3A procurement/CRM demo rows idempotently.

    A received purchase order goes through the real receiving service so the demo
    `wms_movements`, `inventory_ledger` and `inventory_balances` rows stay consistent;
    the second order stays open so the UI can demo one-click receiving.
    """

    def row_id(key: str) -> UUID:
        return uuid5(tenant_id, key)

    supplier = row_id("supplier-led-driver")
    open_po = row_id("po-open")
    received_po = row_id("po-received")
    for sql, params in (
        (
            """INSERT INTO suppliers (id, tenant_id, code, name, contact_name, contact_email,
                country, lead_time_days, payment_terms, rating)
               VALUES (:sid, :tid, 'SUP-LED-01', 'Ningbo Driver Components Co.', 'Li Wei',
                'sales@driver-components.example.com', 'CN', 21, '30% T/T deposit', 4.50)
               ON CONFLICT (tenant_id, code) DO NOTHING""",
            {"sid": supplier, "tid": tenant_id},
        ),
        (
            """INSERT INTO purchase_orders (id, tenant_id, po_no, supplier_id, warehouse_id,
                currency, total_amount, status, expected_arrival_date, notes, created_by)
               VALUES (:poid, :tid, :po_no, :sid, :wid, 'USD', :total, :status,
                (now() + interval '14 days')::date, :notes, :owner)
               ON CONFLICT (tenant_id, po_no) DO NOTHING""",
            {
                "poid": open_po,
                "tid": tenant_id,
                "po_no": "PO-DEMO-1001",
                "sid": supplier,
                "wid": warehouse_id,
                "total": "5820.00",
                "status": "ordered",
                "notes": "COB strip reels, ship with next consolidated container.",
                "owner": owner_id,
            },
        ),
        (
            """INSERT INTO purchase_order_items (tenant_id, purchase_order_id, variant_id,
                sku, qty_ordered, qty_received, unit_cost)
               VALUES (:tid, :poid, :vid, 'LED-COB-24V-5M', 1200, 0, 4.8500)
               ON CONFLICT (tenant_id, purchase_order_id, variant_id) DO NOTHING""",
            {"tid": tenant_id, "poid": open_po, "vid": variant_id},
        ),
        (
            """INSERT INTO purchase_orders (id, tenant_id, po_no, supplier_id, warehouse_id,
                currency, total_amount, status, expected_arrival_date, notes, created_by)
               VALUES (:poid, :tid, :po_no, :sid, :wid, 'USD', :total, 'ordered',
                (now() - interval '3 days')::date, 'Arrived and inspected.', :owner)
               ON CONFLICT (tenant_id, po_no) DO NOTHING""",
            {
                "poid": received_po,
                "tid": tenant_id,
                "po_no": "PO-DEMO-1002",
                "sid": supplier,
                "wid": warehouse_id,
                "total": "1455.00",
                "owner": owner_id,
            },
        ),
        (
            """INSERT INTO purchase_order_items (tenant_id, purchase_order_id, variant_id,
                sku, qty_ordered, qty_received, unit_cost)
               VALUES (:tid, :poid, :vid, 'LED-COB-24V-5M', 300, 0, 4.8500)
               ON CONFLICT (tenant_id, purchase_order_id, variant_id) DO NOTHING""",
            {"tid": tenant_id, "poid": received_po, "vid": variant_id},
        ),
        (
            """INSERT INTO customer_followups (id, tenant_id, customer_id, author_user_id,
                followup_type, summary, next_action_at)
               VALUES (:fid, :tid, :buyer, :owner, 'email',
                'Sent COB strip samples and the IP65 test report to the buyer.',
                now() + interval '2 days')
               ON CONFLICT (id) DO NOTHING""",
            {
                "fid": row_id("followup-hans"),
                "tid": tenant_id,
                "buyer": buyer_id,
                "owner": owner_id,
            },
        ),
        (
            """INSERT INTO task_reminders (id, tenant_id, title, due_at, priority, status,
                linked_entity_type, linked_entity_id, assignee_user_id)
               VALUES (:rid, :tid, :title, now() + interval '3 days', 'high',
                'open', 'customer', :buyer, :owner)
               ON CONFLICT (id) DO NOTHING""",
            {
                "rid": row_id("task-sample"),
                "tid": tenant_id,
                "title": "Confirm 3000K vs 4000K sample preference with Hans",
                "due": "3 days",
                "priority": "high",
                "buyer": buyer_id,
                "owner": owner_id,
            },
        ),
        (
            """INSERT INTO task_reminders (id, tenant_id, title, due_at, priority, status,
                linked_entity_type, linked_entity_id, assignee_user_id)
               VALUES (:rid, :tid, 'Chase Ningbo driver PMC for PO-DEMO-1001 ETD',
                now() + interval '7 days', 'normal', 'open', 'purchase_order', :po, :owner)
               ON CONFLICT (id) DO NOTHING""",
            {"rid": row_id("task-po"), "tid": tenant_id, "po": open_po, "owner": owner_id},
        ),
    ):
        await session.execute(text(sql), params)
    if not await session.scalar(
        text("SELECT 1 FROM wms_movements WHERE tenant_id = :tid LIMIT 1"),
        {"tid": tenant_id},
    ):
        await procurement.receive_purchase_order(
            session,
            tenant_id,
            owner_id,
            received_po,
            ReceivePurchaseOrder(receipt_id=row_id("receipt-demo-1002")),
        )


async def _seed_workspace_demo_data(
    session: AsyncSession,
    *,
    crypto: EnvelopeCryptoService,
    tenant_id: UUID,
    slug: str,
    onboarding: bool,
    write_onboarding_state: bool,
    order_no: str,
    tracking_number: str,
) -> None:
    """Populate one workspace with demo catalogue, channels, inbox and a grounded proposal.

    Shared by the isolated live-E2E workspaces and the `--with-demo-data` demo
    seed so both paths exercise identical rows.
    """
    tid = tenant_id

    def row_id(key: str) -> UUID:
        return uuid5(tid, key)

    stage = OnboardingStage.WORKSPACE_CREATED if onboarding else OnboardingStage.LIVE_PRODUCTION
    steps = [stage.value] if onboarding else [item.value for item in OnboardingStage]
    params = {
        "tid": tid,
        "stage": stage.value,
        "steps": json.dumps(steps),
        "vid": row_id("sku"),
        "pid": row_id("product"),
        "warehouse": row_id("warehouse"),
        "buyer": row_id("hans"),
        "consumer": row_id("elena"),
        "conv": row_id("wismo"),
        "chat": row_id("webchat"),
        "site": f"{slug}-site",
        "order": row_id("shipped"),
        "shipment": row_id("shipment"),
        "order_no": order_no,
        "track": tracking_number,
        "company": row_id("berlin-lighting"),
        "pricing": json.dumps({"quote_pricing": {"incoterm_surcharge_pct": {"FOB": "0"}}}),
        "translation": json.dumps(
            {
                "detected_language": "es",
                "translations": {
                    "en": "Hello, where is my order?",
                    "zh": "你好，我的订单在哪里？",
                },
            }
        ),
        "tiers": json.dumps(
            [
                {"min_qty": 100, "unit_price_usd": "14.50"},
                {"min_qty": 500, "unit_price_usd": "12.00"},
            ]
        ),
    }
    if write_onboarding_state:
        await session.execute(
            text(
                "INSERT INTO onboarding_states (tenant_id, current_stage, "
                "completed_steps, metadata_json) VALUES (:tid, :stage, "
                "CAST(:steps AS jsonb), CAST(:baseline AS jsonb))"
            ),
            {**params, "baseline": json.dumps({"base_currency": "USD"})},
        )
    for sql in (
        "UPDATE tenants SET ai_policy_settings = CAST(:pricing AS jsonb) WHERE id = :tid",
        "INSERT INTO credit_wallets (tenant_id, included_credits_remaining, "
        "billing_cycle_end) VALUES (:tid, 10000, now() + interval '30 days')",
        "INSERT INTO products (id, tenant_id, spu_code, category) "
        "VALUES (:pid, :tid, 'LED-COB-24V', 'LED strips')",
        """INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base,
            base_currency, retail_price_usd, weight_grams, lead_time_days, b2b_moq_tiers)
           VALUES (:vid, :tid, :pid, 'LED-COB-24V-5M', 5, 'USD', 29.90, 400, 7,
            CAST(:tiers AS jsonb))""",
        "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty) "
        "VALUES (:tid, :warehouse, :vid, 4000)",
        """INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email,
            country_code, iana_timezone, preferred_language)
           VALUES (:consumer, :tid, 'b2c_consumer', 'Elena Vance', 'elena@example.com',
            'US', 'America/New_York', 'en')""",
        """INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency,
            fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status,
            payment_status)
           VALUES (:order, :tid, :order_no, 'b2c_retail', :consumer, 'USD', 1, 29.90,
            29.90, 'shipped', 'paid')""",
        """INSERT INTO shipments (id, tenant_id, order_id, tracking_number, carrier_code,
            standardized_status, last_event_at)
           VALUES (:shipment, :tid, :order, :track, 'dhl', 'in_transit', now())""",
    ):
        await session.execute(text(sql), params)
    await DocumentIngestionService().ingest_document(
        session,
        tenant_id=tid,
        title="COB 24V LED strip warranty and IP rating",
        source_type="markdown",
        raw_bytes=(b"The COB 24V LED strip warranty covers defects and the IP rating is IP65."),
    )
    if onboarding:
        return

    for channel, external in (
        ("email", f"{slug}@inbound.mail.example.com"),
        ("shopify", f"{slug}.myshopify.com"),
        ("web_livechat", str(params["site"])),
    ):
        aid = row_id("webchat" if channel == "web_livechat" else channel)
        await session.execute(
            text("""
            INSERT INTO channel_accounts (id, tenant_id, channel_type,
                external_account_id, display_name, status, config)
            VALUES (:id, :tid, :kind, :external, :external, 'active',
                CAST(:config AS jsonb))
        """),
            {
                "id": aid,
                "tid": tid,
                "kind": channel,
                "external": external,
                "config": json.dumps({"warehouse_id": str(params["warehouse"])}),
            },
        )
        await register_channel_routing(
            session,
            channel_type=channel,
            external_account_id=external,
            tenant_id=tid,
            channel_account_id=aid,
        )
        if channel == "email":
            await _upsert_encrypted_credential(
                session,
                crypto,
                tenant_id=tid,
                account_id=aid,
                cred_type="resend_webhook_secret",
                plaintext_secret=os.environ.get("RESEND_WEBHOOK_SECRET", "dev-resend-secret"),
            )
        if channel == "shopify":
            await CredentialVaultHelper(crypto).save_secret_json(
                session,
                tenant_id=tid,
                channel_account_id=aid,
                credential_type="shopify_custom_app",
                payload={
                    "shop_domain": external,
                    "auth_mode": "oauth",
                    "access_token": "demo-token",
                    "client_secret": os.environ.get("SHOPIFY_CLIENT_SECRET", "dev-shopify-secret"),
                },
            )
            await session.execute(
                text("""
                INSERT INTO platform_sku_mappings (tenant_id, variant_id,
                    channel_account_id, external_product_id, external_sku)
                VALUES (:tid, :vid, :aid, 'gid://shopify/Product/1', 'LED-COB-24V-5M')
            """),
                {**params, "aid": aid},
            )
    for sql in (
        """INSERT INTO b2b_companies (id, tenant_id, legal_name, country_code)
           VALUES (:company, :tid, 'Berlin Lighting GmbH', 'DE')""",
        """INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email,
            company_id, country_code, iana_timezone, preferred_language)
           VALUES (:buyer, :tid, 'b2b_contact', 'Hans Weber',
            'hans.weber@berlin-lighting.de', :company, 'DE', 'Europe/Berlin', 'de')""",
        """INSERT INTO customer_identities (tenant_id, customer_id, identity_type,
            identity_value, confidence_level) VALUES
            (:tid, :buyer, 'email', 'hans.weber@berlin-lighting.de', 'deterministic'),
            (:tid, :consumer, 'email', 'elena@example.com', 'deterministic')""",
        """INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id,
            channel_type, conversation_mode, status, subject, last_inbound_at)
           VALUES (:conv, :tid, :consumer, :chat, 'web_livechat', 'b2c_support', 'open',
            'Where is my Shopify order?', now())""",
        """INSERT INTO messages (tenant_id, conversation_id, customer_id, channel_type,
            direction, sender_type, content_type, body_text, channel_metadata,
            source_timestamp)
           VALUES (:tid, :conv, :consumer, 'web_livechat', 'inbound', 'customer', 'text',
            'Hola, ¿dónde está mi pedido?', CAST(:translation AS jsonb), now())""",
    ):
        await session.execute(text(sql), params)
    items = [{"sku": "LED-COB-24V-5M", "qty": 500}]
    matrix = await ErpToolRegistry().calc_b2b_quote_matrix(
        session,
        tid,
        items,
        "USD",
        Incoterm.FOB,
    )
    if not matrix.ok or matrix.data["below_margin_floor"]:
        raise RuntimeError(f"Demo quote is not viable: {matrix.error_code}: {matrix.error_message}")
    await session.execute(
        text("""
        INSERT INTO ai_proposals (tenant_id, capability, proposal_type, target_entity_type,
            target_entity_id, proposed_payload, confidence_score, expires_at)
        VALUES (:tid, 'sales_quoting', 'quote_draft', 'customer', :buyer,
            CAST(:payload AS jsonb), 1, now() + interval '7 days')
    """),
        {
            **params,
            "payload": json.dumps(
                {
                    "items": items,
                    "customer_id": str(params["buyer"]),
                    "quote_currency": "USD",
                    "incoterm": "FOB",
                    "quote_matrix": matrix.data,
                }
            ),
        },
    )
    await _seed_phase3a_demo(
        session,
        tenant_id=tid,
        warehouse_id=params["warehouse"],
        variant_id=params["vid"],
        buyer_id=params["buyer"],
        owner_id=uuid5(NAMESPACE_DNS, "user:owner@quanzhou-led.example.com"),
    )


async def seed_live_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    slug: str,
) -> None:
    """Add isolated demo workspaces without resetting existing rows or onboarding progress."""
    if not re.fullmatch(r"quanzhou-led-e2e-[0-9a-f]{12}", slug):
        raise ValueError("Use a dedicated quanzhou-led-e2e-<12 hex digits> workspace")
    crypto = EnvelopeCryptoService(settings)
    password_hash = hash_password(SEED_DEMO_PASSWORD)
    for onboarding in (False, True):
        workspace = f"{slug}-onboarding" if onboarding else slug
        tid = uuid5(NAMESPACE_DNS, workspace)

        def row_id(key: str, tenant: UUID = tid) -> UUID:
            return uuid5(tenant, key)

        async with tenant_session(session_factory, tid) as session:
            if await session.scalar(text("SELECT id FROM tenants WHERE id = :id"), {"id": tid}):
                continue  # Setup is atomic per tenant; do not rewind a completed scenario.
            await _upsert_tenant(
                session,
                tenant_id=tid,
                slug=workspace,
                name="Quanzhou Smart LED E2E",
                preset="hybrid_factory_b2b_b2c",
                currency="USD",
                timezone="Asia/Shanghai",
            )
            await crypto.provision_tenant_dek(session, tid)
            roles = ("onboarding",) if onboarding else ("owner", "agent", "finance")
            for role in roles:
                await _upsert_user_and_membership(
                    session,
                    tenant_id=tid,
                    email=f"{role}@quanzhou-led.example.com",
                    full_name=f"Demo {role.title()}",
                    role=UserRole.OWNER if role == "onboarding" else UserRole(role),
                    password_hash=password_hash,
                )
            await _seed_workspace_demo_data(
                session,
                crypto=crypto,
                tenant_id=tid,
                slug=workspace,
                onboarding=onboarding,
                write_onboarding_state=True,
                order_no="#SH-1042" if onboarding else "DEMO-SHIPPED",
                tracking_number=f"DHL-{workspace.upper()}",
            )


async def _cli_main(live_slug: str | None, with_demo_data: bool = False) -> None:
    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        if live_slug is not None:
            await seed_live_workspaces(session_factory, settings, live_slug)
            print(f"Seeded isolated live E2E workspaces for {live_slug}")
            return
        summary = await seed_hybrid_factory(session_factory, settings, demo_data=with_demo_data)
        print(
            f"Seeded Tenant A ({summary.tenant_a_slug}: {summary.tenant_a_id}) "
            f"and Tenant B ({summary.tenant_b_slug}: {summary.tenant_b_id})"
            f" {'with demo business data' if with_demo_data else 'accounts only'}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the local hybrid-factory demo")
    parser.add_argument("--live-slug", help="Seed isolated, repeatable live E2E workspaces instead")
    parser.add_argument(
        "--with-demo-data",
        action="store_true",
        help="Also seed the flagship workspace's demo catalogue, channels, inbox and proposal "
        "(used by the docker compose demo-seed profile; skipped if already present)",
    )
    _args = parser.parse_args()
    asyncio.run(_cli_main(_args.live_slug, _args.with_demo_data))
