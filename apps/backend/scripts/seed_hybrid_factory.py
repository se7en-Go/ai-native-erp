from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import NAMESPACE_DNS, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings, get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session
from erp_core.shared.enums import OnboardingStage, UserRole
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


async def _cli_main() -> None:
    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        summary = await seed_hybrid_factory(session_factory, settings)
        print(
            f"Seeded Tenant A ({summary.tenant_a_slug}: {summary.tenant_a_id}) "
            f"and Tenant B ({summary.tenant_b_slug}: {summary.tenant_b_id})"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_cli_main())
