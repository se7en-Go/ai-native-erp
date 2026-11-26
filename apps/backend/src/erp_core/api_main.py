from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

# Composition root: import every domain model module so SQLAlchemy resolves the
# cross-module string foreign keys when it configures the mappers.
import erp_core.ai_engine.mcp_models  # noqa: F401
import erp_core.ai_engine.models  # noqa: F401
import erp_core.ai_engine.notification_models  # noqa: F401
import erp_core.billing.models  # noqa: F401
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.crm.portal_models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.finance.settlement_models  # noqa: F401
import erp_core.growth.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.orders.routing_models  # noqa: F401
import erp_core.social.models  # noqa: F401
import erp_core.supply_chain.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.auto_reply_router import router as ai_auto_reply_router
from erp_core.ai_engine.kb_router import router as kb_router
from erp_core.ai_engine.mcp_router import public_router as mcp_public_router
from erp_core.ai_engine.mcp_router import router as ai_mcp_router
from erp_core.ai_engine.notification_router import router as ai_notification_router
from erp_core.ai_engine.router import router as ai_router
from erp_core.ai_engine.router_llm import TieredModelRouter
from erp_core.ai_engine.runtime import PolicyRuntime
from erp_core.ai_engine.sandbox import router as sandbox_router
from erp_core.ai_engine.semantic_cache import SemanticCacheService
from erp_core.ai_engine.social_caption import AiEngineCaptionProvider
from erp_core.ai_engine.sourcing_copy import AiEngineSourcingCopyRewriter
from erp_core.ai_engine.workforce_router import workforce_router
from erp_core.billing.router import router as billing_router
from erp_core.catalog.router import router as catalog_router
from erp_core.catalog.sourcing_router import router as catalog_sourcing_router
from erp_core.channels.adapters.instagram_dm import instagram_router
from erp_core.channels.adapters.messenger import messenger_router
from erp_core.channels.adapters.telegram import telegram_router
from erp_core.channels.adapters.whatsapp import whatsapp_router
from erp_core.channels.ecommerce.connectors_router import router as connectors_router
from erp_core.channels.ecommerce.inventory_sync_router import router as inventory_sync_router
from erp_core.channels.ecommerce.listing_dispatcher import MarketplaceListingDispatcher
from erp_core.channels.ecommerce.marketplace_connect_router import (
    router as marketplace_connect_router,
)
from erp_core.channels.ecommerce.shopify_router import router as shopify_router
from erp_core.channels.inbox_router import inbox_router
from erp_core.channels.ingress_worker import dlq_router
from erp_core.channels.settings_router import router as channel_settings_router
from erp_core.channels.webhook_router import webhook_router
from erp_core.channels.widget_router import widget_router
from erp_core.crm.portal_router import (
    portal_operator_router,
    portal_public_router,
)
from erp_core.crm.router import router as b2b_router
from erp_core.crm.workbench import router as crm_workbench_router
from erp_core.finance.ap_router import router as finance_ap_router
from erp_core.finance.router import router as finance_router
from erp_core.finance.settlement_router import router as finance_settlement_router
from erp_core.growth.router import router as growth_router
from erp_core.identity.router import customer_router
from erp_core.orders.router import router as orders_router
from erp_core.orders.routing_router import router as order_routing_router
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
from erp_core.social.attribution_router import router as social_attribution_router
from erp_core.social.publishers import HttpSocialPublisherDispatcher
from erp_core.social.router import router as social_router
from erp_core.supply_chain.analytics import router as executive_router
from erp_core.supply_chain.router import supply_router, wms_router
from erp_core.tenancy.audit import ensure_audit_child_rls
from erp_core.tenancy.onboarding import onboarding_router
from erp_core.tenancy.onboarding_dns import router as onboarding_dns_router
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
    app.state.ai_runtime = PolicyRuntime(
        llm_router=TieredModelRouter(semantic_cache=SemanticCacheService(redis_client)),
    )
    # Social copywriting is injected through the social module's CaptionProvider
    # protocol: erp_core.social never imports erp_core.ai_engine (import-linter).
    app.state.social_caption_provider = AiEngineCaptionProvider(
        llm_router=app.state.ai_runtime.llm_router,
        wallet_service=app.state.ai_runtime.wallet_service,
        tool_registry=app.state.ai_runtime.tool_registry,
    )
    # Sourcing copy rewriting is injected through the catalog module's
    # SourcingCopyRewriter protocol for the same import-linter reason: erp_core.catalog
    # must never import erp_core.ai_engine (`catalog_isolated`,
    # `domain_modules_never_import_ai_engine`).
    app.state.sourcing_copy_rewriter = AiEngineSourcingCopyRewriter(
        llm_router=app.state.ai_runtime.llm_router,
        wallet_service=app.state.ai_runtime.wallet_service,
    )
    app.state.storage = storage
    app.state.crypto_service = crypto_service
    # The catalog module must never import erp_core.channels (import-linter
    # `catalog_isolated`), so the live marketplace push is injected here through the
    # `MarketplaceListingPublisher` protocol instead.
    app.state.marketplace_listing_publisher = MarketplaceListingDispatcher()
    # Live social publishing (Instagram/Facebook/TikTok/YouTube/LinkedIn/Pinterest/X/
    # WeChat OA) is injected through the social module's `SocialPublisherPort` protocol.
    app.state.social_publisher = HttpSocialPublisherDispatcher()
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


class ScopedCORSMiddleware:
    """Bypass global CORS rejections on routes that enforce per-account allowed_origins."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_prefixes: tuple[str, ...] = (),
        allow_origins: list[str] | tuple[str, ...] = (),
        allow_origin_regex: str | None = None,
        allow_credentials: bool = False,
        allow_methods: list[str] | tuple[str, ...] = ("GET",),
        allow_headers: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._app = app
        self._excluded_prefixes = excluded_prefixes
        self._cors = CORSMiddleware(
            app,
            allow_origins=allow_origins,
            allow_origin_regex=allow_origin_regex,
            allow_credentials=allow_credentials,
            allow_methods=allow_methods,
            allow_headers=allow_headers,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = str(scope.get("path") or "")
            if any(
                path.startswith(prefix) or path == prefix.rstrip("/")
                for prefix in self._excluded_prefixes
            ):
                await self._app(scope, receive, send)
                return
        await self._cors(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Omnichannel Hybrid ERP API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        ScopedCORSMiddleware,
        excluded_prefixes=("/api/v1/widget/",),
        allow_origins=settings.CORS_ORIGINS,
        allow_origin_regex=r"^chrome-extension://[a-z]{32}$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(tenancy_router)
    app.include_router(onboarding_router)
    app.include_router(onboarding_dns_router)
    app.include_router(sandbox_router)
    app.include_router(channel_settings_router)
    app.include_router(tracking_router, prefix="/api/v1")
    app.include_router(shopify_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(marketplace_connect_router)
    app.include_router(webhook_router)
    app.include_router(dlq_router)
    app.include_router(whatsapp_router)
    app.include_router(messenger_router)
    app.include_router(instagram_router)
    app.include_router(telegram_router)
    app.include_router(widget_router)
    app.include_router(inbox_router)
    app.include_router(customer_router)
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(catalog_sourcing_router)
    app.include_router(b2b_router, prefix="/api/v1")
    # Buyer self-service portal: the operator router mints/revokes magic links, the
    # public router is authenticated solely by the opaque `{token}` path segment.
    app.include_router(portal_operator_router, prefix="/api/v1")
    app.include_router(portal_public_router, prefix="/api/v1")
    app.include_router(crm_workbench_router, prefix="/api/v1")
    app.include_router(supply_router, prefix="/api/v1")
    app.include_router(wms_router, prefix="/api/v1")
    app.include_router(executive_router, prefix="/api/v1")
    app.include_router(inventory_sync_router)
    # `order_routing_router` owns the static `/orders/routing-rules` path, so it must be
    # registered before the dynamic `/orders/{order_id}` routes of `orders_router`.
    app.include_router(order_routing_router)
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(finance_router, prefix="/api/v1")
    app.include_router(finance_ap_router)
    app.include_router(finance_settlement_router)
    app.include_router(kb_router)
    app.include_router(growth_router, prefix="/api/v1")
    app.include_router(social_router, prefix="/api/v1")
    app.include_router(social_attribution_router)
    app.include_router(ai_router)
    app.include_router(ai_mcp_router)
    app.include_router(mcp_public_router)
    app.include_router(ai_auto_reply_router)
    app.include_router(workforce_router)
    app.include_router(ai_notification_router)
    app.include_router(billing_router)

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
