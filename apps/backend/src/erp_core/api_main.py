from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

# Composition root: import every domain model module so SQLAlchemy resolves the
# cross-module string foreign keys when it configures the mappers.
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.router import router as catalog_router
from erp_core.channels.adapters.whatsapp import whatsapp_router
from erp_core.channels.ecommerce.connectors_router import router as connectors_router
from erp_core.channels.ecommerce.shopify_router import router as shopify_router
from erp_core.channels.inbox_router import inbox_router
from erp_core.channels.ingress_worker import dlq_router
from erp_core.channels.webhook_router import webhook_router
from erp_core.channels.widget_router import widget_router
from erp_core.crm.router import router as b2b_router
from erp_core.finance.router import router as finance_router
from erp_core.identity.router import customer_router
from erp_core.orders.router import router as orders_router
from erp_core.orders.tracking_router import router as tracking_router
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
)
from erp_core.shared.errors import register_exception_handlers
from erp_core.shared.redis_client import create_redis_client
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import ensure_audit_child_rls
from erp_core.tenancy.onboarding import onboarding_router
from erp_core.tenancy.router import router as tenancy_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client = create_redis_client(settings)
    storage = ObjectStorageService(settings)
    crypto_service = EnvelopeCryptoService(settings)
    runtime_engine = create_runtime_engine(settings)
    migrator_engine = create_migrator_engine(settings)
    session_factory = create_session_factory(runtime_engine)
    migrator_sessions = create_session_factory(migrator_engine)

    app.state.settings = settings
    app.state.redis = redis_client
    app.state.storage = storage
    app.state.crypto_service = crypto_service
    app.state.runtime_engine = runtime_engine
    app.state.session_factory = session_factory
    app.state.broker_session_factory = migrator_sessions
    try:
        async with migrator_sessions() as session, session.begin():
            await ensure_audit_child_rls(session)
        try:
            yield
        finally:
            await runtime_engine.dispose()
            await redis_client.aclose()
    finally:
        await migrator_engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Omnichannel Hybrid ERP API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(tenancy_router)
    app.include_router(onboarding_router)
    app.include_router(tracking_router, prefix="/api/v1")
    app.include_router(shopify_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(webhook_router)
    app.include_router(dlq_router)
    app.include_router(whatsapp_router)
    app.include_router(widget_router)
    app.include_router(inbox_router)
    app.include_router(customer_router)
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(b2b_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(finance_router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            pong = await request.app.state.redis.ping()
            checks["redis"] = "ok" if pong else "error"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"

        try:
            async with system_session(request.app.state.session_factory) as session:
                val = await session.scalar(text("SELECT 1"))
                checks["postgres"] = "ok" if val == 1 else "error"
        except Exception as exc:
            checks["postgres"] = f"error: {type(exc).__name__}"

        is_ready = all(val == "ok" for val in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "degraded", "checks": checks},
        )

    return app


app = create_app()
