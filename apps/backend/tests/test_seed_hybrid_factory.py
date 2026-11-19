from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT_PATH = REPO_ROOT / "apps" / "backend" / "scripts" / "seed_hybrid_factory.py"


def _load_seed_module():
    assert SEED_SCRIPT_PATH.exists(), "seed_hybrid_factory.py does not exist"
    spec = importlib.util.spec_from_file_location("seed_hybrid_factory", SEED_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_seed_hybrid_factory_is_idempotent_and_isolated(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed_mod = _load_seed_module()
    settings = get_settings()

    summary_1 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)
    summary_2 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)

    assert summary_1.tenant_a_id == summary_2.tenant_a_id
    assert summary_1.tenant_b_id == summary_2.tenant_b_id
    assert summary_2.tenant_a_slug == "quanzhou-led-factory"
    assert summary_2.tenant_b_slug == "shenzhen-audio-dtc"

    crypto = EnvelopeCryptoService(settings)

    async with tenant_session(runtime_session_factory, summary_2.tenant_a_id) as session:
        tenants = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [row.slug for row in tenants] == ["quanzhou-led-factory"]

        members = (await session.execute(text("SELECT role FROM tenant_memberships"))).all()
        assert sorted(row.role for row in members) == sorted(
            ["owner", "admin", "manager", "agent", "finance"]
        )

        creds = (
            await session.execute(
                text(
                    "SELECT channel_account_id, credential_type, ciphertext, nonce, "
                    "auth_tag, key_version "
                    "FROM channel_credentials ORDER BY credential_type"
                )
            )
        ).all()
        assert len(creds) == 3

        first = creds[0]
        decrypted = await crypto.decrypt_secret(
            session,
            summary_2.tenant_a_id,
            UUID(str(first.channel_account_id)),
            str(first.credential_type),
            bytes(first.ciphertext),
            bytes(first.nonce),
            bytes(first.auth_tag),
            int(first.key_version),
        )
        assert decrypted

        stage = await session.scalar(text("SELECT current_stage FROM onboarding_states"))
        assert stage == "LiveProduction"

    async with tenant_session(runtime_session_factory, summary_2.tenant_b_id) as session:
        tenants_b = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [row.slug for row in tenants_b] == ["shenzhen-audio-dtc"]

        members_b = (await session.execute(text("SELECT role FROM tenant_memberships"))).all()
        assert [row.role for row in members_b] == ["owner"]

        creds_b = (await session.execute(text("SELECT id FROM channel_credentials"))).all()
        assert len(creds_b) == 1

        with pytest.raises(AppError) as err:
            await crypto.decrypt_secret(
                session,
                summary_2.tenant_b_id,
                UUID(str(first.channel_account_id)),
                str(first.credential_type),
                bytes(first.ciphertext),
                bytes(first.nonce),
                bytes(first.auth_tag),
                int(first.key_version),
            )
        assert err.value.code == "ERR_INVALID_CREDENTIAL_TAG"

    owner_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert owner_login.status_code == 200
    assert owner_login.json()["principal"]["role"] == "owner"
    assert owner_login.json()["principal"]["can_view_cost"] is True

    agent_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "agent@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert agent_login.status_code == 200
    assert agent_login.json()["principal"]["role"] == "agent"
    assert agent_login.json()["principal"]["can_view_cost"] is False
