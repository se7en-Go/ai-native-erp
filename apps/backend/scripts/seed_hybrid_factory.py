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

from erp_core.ai_engine.ingestion import DocumentIngestionService
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.channels.ecommerce.base import CredentialVaultHelper, register_channel_routing
from erp_core.shared.config import Settings, get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session
from erp_core.shared.enums import Incoterm, OnboardingStage, UserRole
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
) -> SeedSummary:
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
            stage = (
                OnboardingStage.WORKSPACE_CREATED if onboarding else OnboardingStage.LIVE_PRODUCTION
            )
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
                "site": f"{workspace}-site",
                "order": row_id("shipped"),
                "shipment": row_id("shipment"),
                "order_no": "#SH-1042" if onboarding else "DEMO-SHIPPED",
                "track": f"DHL-{workspace.upper()}",
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
            for sql in (
                "INSERT INTO onboarding_states (tenant_id, current_stage, completed_steps, "
                "metadata_json) VALUES (:tid, :stage, CAST(:steps AS jsonb), "
                '\'{"base_currency":"USD"}\'::jsonb)',
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
                raw_bytes=(
                    b"The COB 24V LED strip warranty covers defects and the IP rating is IP65."
                ),
            )
            if onboarding:
                continue

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
                        plaintext_secret=os.environ.get(
                            "RESEND_WEBHOOK_SECRET", "dev-resend-secret"
                        ),
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
                            "client_secret": os.environ.get(
                                "SHOPIFY_CLIENT_SECRET", "dev-shopify-secret"
                            ),
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
                raise RuntimeError(
                    f"Demo quote is not viable: {matrix.error_code}: {matrix.error_message}"
                )
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


async def _cli_main(live_slug: str | None) -> None:
    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        if live_slug is not None:
            await seed_live_workspaces(session_factory, settings, live_slug)
            print(f"Seeded isolated live E2E workspaces for {live_slug}")
            return
        summary = await seed_hybrid_factory(session_factory, settings)
        print(
            f"Seeded Tenant A ({summary.tenant_a_slug}: {summary.tenant_a_id}) "
            f"and Tenant B ({summary.tenant_b_slug}: {summary.tenant_b_id})"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the local hybrid-factory demo")
    parser.add_argument("--live-slug", help="Seed isolated, repeatable live E2E workspaces instead")
    asyncio.run(_cli_main(parser.parse_args().live_slug))
