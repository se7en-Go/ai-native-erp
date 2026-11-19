from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from erp_core.shared.config import Settings


class Base(DeclarativeBase):
    """Shared SQLAlchemy 2.0 Declarative Base across modular monolith domains."""


def create_runtime_engine(settings: Settings) -> AsyncEngine:
    """Runtime engine for app_runtime connecting through PgBouncer transaction pool (6432).

    Disables asyncpg prepared statement caching for full PgBouncer transaction pooling safety.
    asyncpg 0.32 exposes only ``statement_cache_size``; that is the prepared-statement cache.
    """
    return create_async_engine(
        settings.DATABASE_RUNTIME_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
        },
    )


def create_migrator_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_MIGRATOR_URL,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped RLS database session using SET LOCAL via set_config(..., true)."""
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def system_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped session with app.current_tenant_id explicitly cleared to fail closed."""
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', '', true)"),
        )
        yield session
