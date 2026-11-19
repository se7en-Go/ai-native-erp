from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError


async def _create_tenant(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    tid = uuid4()
    async with tenant_session(session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
            {"id": str(tid), "slug": f"crypto-{tid.hex[:8]}", "name": "Crypto Test Tenant"},
        )
    return tid, uuid4()


async def test_envelope_encrypt_decrypt_roundtrip_and_aad_binding(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    crypto = EnvelopeCryptoService(get_settings())
    tenant_a, account_a = await _create_tenant(runtime_session_factory)
    tenant_b, _ = await _create_tenant(runtime_session_factory)
    secret_value = "shpat_live_quanzhou_led_99887766"

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        key_ver = await crypto.provision_tenant_dek(session, tenant_a)
        assert key_ver == 1
        # Idempotent re-provisioning returns existing version
        assert await crypto.provision_tenant_dek(session, tenant_a) == 1

        ciphertext, nonce, auth_tag, used_ver = await crypto.encrypt_secret(
            session, tenant_a, account_a, "oauth_token", secret_value
        )
        assert used_ver == 1
        assert len(nonce) == 12
        assert len(auth_tag) == 16
        assert secret_value.encode("utf-8") not in ciphertext

        decrypted = await crypto.decrypt_secret(
            session, tenant_a, account_a, "oauth_token", ciphertext, nonce, auth_tag, used_ver
        )
        assert decrypted == secret_value

        # Wrong credential_type (tampered AAD) MUST fail GCM verification
        with pytest.raises(AppError) as err_type:
            await crypto.decrypt_secret(
                session,
                tenant_a,
                account_a,
                "imap_password",
                ciphertext,
                nonce,
                auth_tag,
                used_ver,
            )
        assert err_type.value.code == "ERR_INVALID_CREDENTIAL_TAG"
        assert isinstance(err_type.value.__cause__, InvalidTag)

        # Wrong account_id (tampered AAD) MUST fail GCM verification
        with pytest.raises(AppError) as err_acct:
            await crypto.decrypt_secret(
                session, tenant_a, uuid4(), "oauth_token", ciphertext, nonce, auth_tag, used_ver
            )
        assert isinstance(err_acct.value.__cause__, InvalidTag)

    # Tenant B attempting to decrypt Tenant A's ciphertext with Tenant B's DEK/AAD MUST fail
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await crypto.provision_tenant_dek(session, tenant_b)
        with pytest.raises(AppError) as err_cross:
            await crypto.decrypt_secret(
                session, tenant_b, account_a, "oauth_token", ciphertext, nonce, auth_tag, used_ver
            )
        assert err_cross.value.code == "ERR_INVALID_CREDENTIAL_TAG"
        assert isinstance(err_cross.value.__cause__, InvalidTag)


async def test_crypto_shred_tenant_renders_backup_ciphertext_permanently_unreadable(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    crypto = EnvelopeCryptoService(get_settings())
    tenant_id, account_id = await _create_tenant(runtime_session_factory)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        ciphertext, nonce, auth_tag, key_ver = await crypto.encrypt_secret(
            session, tenant_id, account_id, "byok_api_key", "sk-live-byok-key-12345"
        )
        await session.execute(
            text(
                "INSERT INTO channel_credentials "
                "(id, tenant_id, channel_account_id, credential_type, ciphertext, nonce, "
                "auth_tag, key_version) "
                "VALUES (:id, :tid, :aid, 'byok_api_key', :ct, :nonce, :tag, :kv)"
            ),
            {
                "id": str(uuid4()),
                "tid": str(tenant_id),
                "aid": str(account_id),
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kv": key_ver,
            },
        )

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await crypto.crypto_shred_tenant(session, tenant_id)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        key_count = await session.scalar(text("SELECT count(*) FROM tenant_keys"))
        cred_count = await session.scalar(text("SELECT count(*) FROM channel_credentials"))
        assert key_count == 0
        assert cred_count == 0

        with pytest.raises(AppError) as err_shred:
            await crypto.decrypt_secret(
                session,
                tenant_id,
                account_id,
                "byok_api_key",
                ciphertext,
                nonce,
                auth_tag,
                key_ver,
            )
        assert err_shred.value.code == "ERR_TENANT_KEY_REVOKED"
        assert err_shred.value.status_code == 403
