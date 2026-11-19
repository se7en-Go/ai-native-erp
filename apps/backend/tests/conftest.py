from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_MIGRATOR_URL",
    "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas",
)
os.environ.setdefault(
    "DATABASE_RUNTIME_URL",
    "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "erp-assets")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault(
    "MASTER_KEK_HEX",
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-minimum-32-bytes-long")


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    from erp_core.api_main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
async def runtime_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_runtime_engine, create_session_factory

    engine = create_runtime_engine(get_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def migrator_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_migrator_engine, create_session_factory

    engine = create_migrator_engine(get_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
