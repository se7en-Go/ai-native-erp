"""Shred must revoke DEKs cached by other EnvelopeCryptoService instances."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError


async def test_crypto_shred_denies_independent_cached_instances(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = get_settings()
    writer = EnvelopeCryptoService(settings)
    reader = EnvelopeCryptoService(settings)
    tenant_id = uuid4()
    account_id = uuid4()
    secret = "secret-before-shred"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Shred Cache')"),
            {"id": str(tenant_id), "slug": f"shred-{tenant_id.hex[:8]}"},
        )
        ciphertext, nonce, auth_tag, key_version = await writer.encrypt_secret(
            session, tenant_id, account_id, "oauth_token", secret
        )
        warmed = await reader.decrypt_secret(
            session, tenant_id, account_id, "oauth_token", ciphertext, nonce, auth_tag, key_version
        )
        assert warmed == secret

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await writer.crypto_shred_tenant(session, tenant_id)

    for instance in (writer, reader):
        with pytest.raises(AppError) as err:
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                await instance.decrypt_secret(
                    session,
                    tenant_id,
                    account_id,
                    "oauth_token",
                    ciphertext,
                    nonce,
                    auth_tag,
                    key_version,
                )
        assert err.value.code == "ERR_TENANT_KEY_REVOKED"
        assert err.value.status_code == 403
