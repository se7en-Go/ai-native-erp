"""PG17 + Redis end-to-end coverage for the marketplace buyer-IM pipeline.

Every test here runs against the **real** PostgreSQL 17 database (tenant RLS forced, non
superuser ``app_runtime``) and the **real** Redis instance the worker uses. The only fake
is outbound marketplace HTTP: ``httpx.MockTransport`` answers the signed buyer-messaging
API of each marketplace, so no test can reach a live platform.

Covered end to end:

A. Shopee buyer chat full round-trip —
   ``POST /api/v1/webhooks/im/shopee`` (real HMAC over the raw body) -> receipt in
   ``raw_webhook_events`` -> ``worker_main`` drain routes the IM receipt into
   ``process_raw_webhook_event`` -> ``customers`` / ``customer_identities
   (identity_type='marketplace_buyer_id')`` / ``conversations(channel_type='shopee')`` /
   ``messages(direction='inbound')`` -> grounded ``AiProposalGeneratorService`` draft ->
   human agent reply through ``InboxService.send_message`` dispatched by
   ``send_outbound_message`` onto the signed Shopee ``/api/v2/sellerchat/send_message``
   call, ending ``delivery_status='sent'`` with the platform message id persisted.
B. Ozon, Mercado Libre and eBay — the same ingress -> Inbox -> outbound loop for one
   existing and two newly onboarded marketplaces, so the twelve-channel wiring cannot
   regress to "Shopee only".
C. Non-regression — an order push on the Shopee storefront endpoint is *not* Inbox work:
   ``is_marketplace_im_event`` returns ``False`` and the receipt is acknowledged without
   creating any conversation or message.
D. Topology guard — the three marketplaces that ``worker_main`` does not drain are the
   only ones added to the ingress worker, so one Redis stream never gets two consumers.

Teardown removes every tenant row, webhook receipt and Redis key these tests create,
including the artifacts a failing run could leave behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.worker_main as worker_main
from erp_core.ai_engine.proposal_generator import AiProposalGeneratorService
from erp_core.ai_engine.router_llm import LlmProviderResponse
from erp_core.api_main import create_app
from erp_core.channels.adapters.base import AdapterRegistry, OutboundMessageRequest
from erp_core.channels.adapters.marketplace_im import (
    MARKETPLACE_IM_CHANNELS,
    build_marketplace_im_adapters,
)
from erp_core.channels.ecommerce.marketplace_connect_router import (
    provision_marketplace_account,
)
from erp_core.channels.ecommerce.shopee import sign_shopee_request
from erp_core.channels.inbox_schemas import SendInboxMessageRequest
from erp_core.channels.inbox_service import InboxService
from erp_core.channels.ingress_worker import (
    _WORKER_CHANNELS,
    process_raw_webhook_event,
    send_outbound_message,
)
from erp_core.channels.webhook_router import (
    OUTBOUND_STREAM_KEY,
    build_default_adapter_registry,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.redis_client import create_redis_client
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

EVENT_PREFIX = "marketplace-im-e2e"
TEST_EMAIL_DOMAIN = "marketplace-im.example.com"
DEFERRED_QUEUE = "queue:webhooks:deferred"
DELAYED_OUTBOUND_QUEUE = "queue:outbound:delayed"

#: Channels the ingress worker consumes because `worker_main` does not drain them.
INGRESS_ONLY_IM_CHANNELS: tuple[ChannelType, ...] = (
    ChannelType.AMAZON_SP,
    ChannelType.EBAY,
    ChannelType.ALIBABA_EMAIL,
)

BUYER_QUESTION = (
    "Hi, please quote your best FOB price and MOQ for SKU LED-SPOT-12W, we need 500 pcs."
)
AGENT_REPLY = "Thanks for reaching out — USD 12.50/pc FOB at 500 pcs, 500 pcs in stock."
PRODUCT_SKU = "LED-SPOT-12W"
RETAIL_PRICE_USD = "12.50"
ON_HAND_QTY = 620


def _admin_url() -> str:
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


#: Tenant-owned rows, deleted children-first so no FK blocks the teardown.
_TENANT_DELETES: tuple[str, ...] = (
    "DELETE FROM dead_letter_events WHERE tenant_id = :tid",
    "DELETE FROM ai_proposals WHERE tenant_id = :tid",
    "DELETE FROM messages WHERE tenant_id = :tid",
    "DELETE FROM conversations WHERE tenant_id = :tid",
    "DELETE FROM customer_identities WHERE tenant_id = :tid",
    "DELETE FROM customers WHERE tenant_id = :tid",
    "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
    "DELETE FROM inventory_balances WHERE tenant_id = :tid",
    "DELETE FROM product_variants WHERE tenant_id = :tid",
    "DELETE FROM products WHERE tenant_id = :tid",
    "DELETE FROM channel_credentials WHERE tenant_id = :tid",
    "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
    "DELETE FROM raw_webhook_events WHERE tenant_id = :tid",
    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
    "DELETE FROM audit_events WHERE tenant_id = :tid",
    "DELETE FROM warehouses WHERE tenant_id = :tid",
    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
)


# --------------------------------------------------------------------------------------
# Fake marketplace APIs
# --------------------------------------------------------------------------------------


class MarketplaceFleet:
    """``httpx.MockTransport`` handler speaking each marketplace's buyer-message API."""

    #: Platform -> (path fragment, JSON response body). The adapters read the platform
    #: message id out of the response, so every body carries one.
    RESPONSES: dict[str, tuple[str, dict[str, Any]]] = {
        "shopee": (
            "/api/v2/sellerchat/send_message",
            {"error": "", "message": "", "request_id": "req-im", "message_id": "shopee-out-1"},
        ),
        "ozon": ("/v1/chat/send/message", {"message_id": "ozon-out-1"}),
        "mercado_libre": (
            "/messages/packs/",
            {"message_id": "meli-out-1"},
        ),
        "ebay": (
            "/commerce/notification/v1/member_message",
            {"messageId": "ebay-out-1"},
        ),
    }

    def __init__(self) -> None:
        self.calls: dict[str, list[httpx.Request]] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        platform = next(
            (key for key, (fragment, _) in self.RESPONSES.items() if fragment in str(request.url)),
            None,
        )
        assert platform is not None, f"unexpected marketplace call {request.url}"
        self.calls.setdefault(platform, []).append(request)
        return httpx.Response(200, json=self.RESPONSES[platform][1])

    def last(self, platform: str) -> httpx.Request:
        assert self.calls.get(platform), f"no outbound {platform} call was recorded"
        return self.calls[platform][-1]


class _OfflineLlm:
    """Deterministic LLM stub: ``has_key`` False forces the grounded local draft path."""

    def has_key(self, provider: str) -> bool:
        return False

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        raise AssertionError("offline client must not be called")


# --------------------------------------------------------------------------------------
# World: real engines, real Redis, one seeded tenant
# --------------------------------------------------------------------------------------


@dataclass
class ImWorld:
    factory: Factory
    broker_factory: Factory
    redis: Redis
    tenant_id: UUID
    user_id: UUID
    principal: CurrentPrincipal
    created: list[UUID] = field(default_factory=list)
    streams: set[str] = field(default_factory=set)
    raw_event_ids: set[str] = field(default_factory=set)
    stream_keys: set[str] = field(default_factory=set)


def _hex_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _raw_body(payload: dict[str, Any]) -> bytes:
    """Compact, key-sorted body so a digest of it is stable across processes."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


@contextlib.asynccontextmanager
async def _admin_engine() -> AsyncIterator[Any]:
    engine = create_async_engine(_admin_url())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _purge_redis(world: ImWorld) -> None:
    """Drop only the stream entries and queue members this suite created."""
    for stream_key in world.stream_keys:
        entries = await world.redis.xrange(stream_key) or []
        for entry_id, fields in entries:
            raw = fields.get("raw_event_id") if fields else None
            if isinstance(raw, bytes):
                raw = raw.decode()
            if raw and raw in world.raw_event_ids:
                await world.redis.xdel(stream_key, entry_id)
    for queue in (DEFERRED_QUEUE, DELAYED_OUTBOUND_QUEUE):
        members = await world.redis.zrange(queue, 0, -1) or []
        for member in members:
            decoded = member.decode() if isinstance(member, bytes) else member
            if str(world.tenant_id) in decoded:
                await world.redis.zrem(queue, member)
    for account_id in world.streams:
        for suffix in ("", ":nopair"):
            await world.redis.delete(f"bucket:acct:{account_id}{suffix}")


async def _cleanup_tenants(world: ImWorld) -> None:
    await _purge_redis(world)
    async with _admin_engine() as engine, engine.begin() as connection:
        for tenant_id in world.created:
            for statement in _TENANT_DELETES:
                await connection.execute(text(statement), {"tid": tenant_id})
            await connection.execute(
                text("DELETE FROM users WHERE email LIKE :pattern"),
                {"pattern": f"%{tenant_id.hex}@{TEST_EMAIL_DOMAIN}"},
            )
            await connection.execute(
                text("DELETE FROM users WHERE email LIKE :pattern"),
                {"pattern": f"%{tenant_id.hex[:8]}%@{TEST_EMAIL_DOMAIN}"},
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id}
            )
        # Storefront receipts may carry a NULL tenant (routing happens after the
        # signature check), so they are swept by the per-run event-id prefix instead.
        await connection.execute(
            text("DELETE FROM raw_webhook_events WHERE external_event_id LIKE :pattern"),
            {"pattern": f"{EVENT_PREFIX}-%"},
        )


@contextlib.asynccontextmanager
async def _im_world(label: str) -> AsyncIterator[ImWorld]:
    settings = get_settings()
    runtime_engine = create_runtime_engine(settings)
    broker_engine = create_migrator_engine(settings)
    factory = create_session_factory(runtime_engine)
    broker_factory = create_session_factory(broker_engine)
    redis = create_redis_client(settings)
    tenant_id = uuid4()
    user_id = uuid4()
    world = ImWorld(
        factory=factory,
        broker_factory=broker_factory,
        redis=redis,
        tenant_id=tenant_id,
        user_id=user_id,
        principal=CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            email=f"{label.lower()}-owner@{TEST_EMAIL_DOMAIN}",
        ),
    )
    world.created.append(world.tenant_id)
    try:
        async with tenant_session(factory, world.tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, base_currency, ai_policy_settings) "
                    "VALUES (:id, :slug, :name, 'USD', CAST(:policy AS jsonb))"
                ),
                {
                    "id": world.tenant_id,
                    "slug": f"{label.lower()}-{world.tenant_id.hex[:8]}",
                    "name": f"{label} Co",
                    "policy": json.dumps(
                        {
                            "quote_pricing": {
                                "incoterm_surcharge_pct": {
                                    "EXW": "0",
                                    "FOB": "0",
                                    "CIF": "0.045",
                                    "DDP": "0.09",
                                }
                            }
                        }
                    ),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, 'IM Owner', 'hash')"
                ),
                {
                    "id": world.user_id,
                    "email": f"{world.tenant_id.hex}@{TEST_EMAIL_DOMAIN}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                    "VALUES (:tid, :uid, 'owner', true)"
                ),
                {"tid": world.tenant_id, "uid": world.user_id},
            )
        yield world
    finally:
        try:
            await _cleanup_tenants(world)
        finally:
            await redis.aclose()
            await runtime_engine.dispose()
            await broker_engine.dispose()


@pytest.fixture
async def shopee_world() -> AsyncIterator[ImWorld]:
    async with _im_world("ShopeeIM") as world:
        yield world


@pytest.fixture
async def fleet_world() -> AsyncIterator[ImWorld]:
    async with _im_world("FleetIM") as world:
        yield world


@contextlib.asynccontextmanager
async def _running_app(
    principal_holder: list[CurrentPrincipal],
    transport: httpx.MockTransport,
) -> AsyncIterator[AsyncClient]:
    """Real ``create_app()`` (real runtime DB + Redis) with outbound HTTP mocked."""
    app = create_app()
    app.state.marketplace_http_transport = transport
    app.dependency_overrides[get_current_principal] = lambda: principal_holder[0]
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def _seed_catalog(session: AsyncSession, tenant_id: UUID) -> UUID:
    """One Shopify-free product SKU with a retail price and live stock for grounding."""
    warehouse_id = uuid4()
    product_id = uuid4()
    variant_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city, is_active) "
            "VALUES (:id, :tid, 'XM-01', 'Xiamen Main', 'CN', 'Xiamen', true)"
        ),
        {"id": warehouse_id, "tid": tenant_id},
    )
    await session.execute(
        text(
            "INSERT INTO products (id, tenant_id, spu_code, category, hs_code) "
            "VALUES (:pid, :tid, 'SPU-SPOT-12W', 'LED Spotlight', '9405409000')"
        ),
        {"pid": product_id, "tid": tenant_id},
    )
    await session.execute(
        text(
            "INSERT INTO product_variants (id, tenant_id, product_id, sku, base_currency, "
            "cost_price_base, retail_price_usd, weight_grams, lead_time_days, b2b_moq_tiers) "
            "VALUES (:vid, :tid, :pid, :sku, 'USD', 7.1000, :retail, 210, 12, '[]'::jsonb)"
        ),
        {
            "vid": variant_id,
            "tid": tenant_id,
            "pid": product_id,
            "sku": PRODUCT_SKU,
            "retail": RETAIL_PRICE_USD,
        },
    )
    await session.execute(
        text(
            "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
            "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, :on_hand, 20)"
        ),
        {
            "tid": tenant_id,
            "wid": warehouse_id,
            "vid": variant_id,
            "on_hand": ON_HAND_QTY,
        },
    )
    return variant_id


async def _connect_shopee(client: AsyncClient, *, shop_id: str, partner_key: str) -> str:
    response = await client.post(
        "/api/v1/channels/shopee/connect",
        json={
            "display_name": f"QZLED Shopee {shop_id}",
            "shop_id": shop_id,
            "partner_id": "2010123",
            "partner_key": partner_key,
            "access_token": f"shopee-access-token-{shop_id}",
            "region": "SG",
            "fulfillment_mode": "standard",
        },
    )
    assert response.status_code == 201, response.text
    assert partner_key not in response.text
    return str(response.json()["channel_account_id"])


@dataclass(frozen=True)
class ImChannelCase:
    """One marketplace buyer-IM channel wired through the shared pipeline."""

    channel_type: ChannelType
    slug: str
    external_account_id: str
    credentials: dict[str, Any]
    webhook_secret: str
    payload: dict[str, Any]
    headers: dict[str, str]
    signature_header: str
    expected_event_id: str
    expected_thread: str
    fleet_platform: str
    expected_outbound_message_id: str


OZON_CASE = ImChannelCase(
    channel_type=ChannelType.OZON,
    slug="ozon",
    external_account_id="oz-client-e2e",
    credentials={"client_id": "oz-client-e2e", "api_key": "ozon-api-key-e2e"},
    webhook_secret="ozon-api-key-e2e",
    payload={
        "client_id": "oz-client-e2e",
        "chat_id": "oz-chat-e2e",
        "message_id": "oz-msg-e2e",
        "seller_id": "oz-client-e2e",
        "sender": {"type": "customer", "id": "oz-buyer-e2e", "name": "Ivan"},
        "content": {"text": BUYER_QUESTION},
    },
    headers={"x-ozon-client-id": "oz-client-e2e", "x-ozon-topic": "chat_message"},
    signature_header="x-ozon-signature",
    expected_event_id="oz-msg-e2e",
    expected_thread="oz-chat-e2e",
    fleet_platform="ozon",
    expected_outbound_message_id="ozon-out-1",
)

MERCADO_LIBRE_CASE = ImChannelCase(
    channel_type=ChannelType.MERCADO_LIBRE,
    slug="mercado-libre",
    external_account_id="ml-seller-e2e",
    credentials={
        "seller_id": "ml-seller-e2e",
        "access_token": "meli-access-token-e2e",
        "webhook_secret": "meli-webhook-secret-e2e",
    },
    webhook_secret="meli-webhook-secret-e2e",
    payload={
        "user_id": "ml-seller-e2e",
        "topic": "messages",
        "conversation_id": "ml-conv-e2e",
        "message": {
            "id": "ml-msg-e2e",
            "text": BUYER_QUESTION,
            "from": {"user_id": "ml-buyer-e2e", "name": "Ana"},
        },
    },
    headers={"x-meli-seller-id": "ml-seller-e2e", "x-meli-topic": "messages"},
    signature_header="x-signature",
    expected_event_id="ml-msg-e2e",
    expected_thread="ml-conv-e2e",
    fleet_platform="mercado_libre",
    expected_outbound_message_id="meli-out-1",
)

EBAY_CASE = ImChannelCase(
    channel_type=ChannelType.EBAY,
    slug="ebay",
    external_account_id="eb-seller-e2e",
    credentials={
        "seller_username": "eb-seller-e2e",
        "access_token": "ebay-access-token-e2e",
        "client_secret": "ebay-client-secret-e2e",
    },
    webhook_secret="ebay-client-secret-e2e",
    payload={
        "seller_username": "eb-seller-e2e",
        "notification": {
            "data": {
                "messageId": "eb-msg-e2e",
                "senderId": "eb-buyer-e2e",
                "conversationId": "eb-conv-e2e",
                "messageText": BUYER_QUESTION,
                "createdAt": 1730000002,
            }
        },
    },
    headers={"x-ebay-seller-username": "eb-seller-e2e", "topic": "member_message"},
    signature_header="x-ebay-signature",
    expected_event_id="eb-msg-e2e",
    expected_thread="eb-conv-e2e",
    fleet_platform="ebay",
    expected_outbound_message_id="ebay-out-1",
)

FLEET_CASES: tuple[ImChannelCase, ...] = (OZON_CASE, MERCADO_LIBRE_CASE, EBAY_CASE)


async def _process_ingress_receipt(world: ImWorld, raw_event_id: UUID) -> str:
    """Exactly what the ingress worker's stream consumer does for one receipt."""
    return await process_raw_webhook_event(
        session_factory=world.factory,
        redis=world.redis,
        raw_event_id=raw_event_id,
        broker_session_factory=world.broker_factory,
    )


async def _provision(world: ImWorld, case: ImChannelCase) -> UUID:
    async with tenant_session(world.factory, world.tenant_id) as session:
        connection = await provision_marketplace_account(
            session,
            tenant_id=world.tenant_id,
            channel_type=case.channel_type.value,
            external_account_id=case.external_account_id,
            display_name=f"QZLED {case.channel_type.value}",
            secret_payload=dict(case.credentials),
            config={},
            actor_id=str(world.user_id),
        )
    world.streams.add(f"{case.channel_type.value}:{case.external_account_id}")
    return UUID(str(connection.channel_account_id))


def _im_registry(redis: Redis, transport: httpx.MockTransport) -> AdapterRegistry:
    """Default registry with every marketplace IM adapter bound to the mock transport."""
    registry = build_default_adapter_registry(redis)
    for channel_type, adapter in build_marketplace_im_adapters(http_transport=transport).items():
        registry.register(channel_type.value, adapter)
        registry.register(channel_type.value.replace("_", "-"), adapter)
    return registry


async def _queued_outbound(
    world: ImWorld, message_id: UUID
) -> OutboundMessageRequest:
    """Read the exact request `InboxService` queued for this message."""
    entries = await world.redis.xrange(OUTBOUND_STREAM_KEY, count=500) or []
    for _entry_id, fields in entries:
        raw = fields.get("payload") if fields else None
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not raw:
            continue
        data = json.loads(raw)
        if data.get("message_id") == str(message_id):
            return OutboundMessageRequest.model_validate(data)
    raise AssertionError(f"no outbound stream entry queued for message {message_id}")


async def _send_agent_reply(
    world: ImWorld,
    conversation_id: UUID,
    fleet: MarketplaceFleet,
) -> tuple[UUID, str]:
    """Human agent reply -> outbound queue -> signed marketplace HTTP call."""
    async with tenant_session(world.factory, world.tenant_id) as session:
        queued = await InboxService().send_message(
            session,
            world.redis,
            world.tenant_id,
            world.principal,
            conversation_id,
            SendInboxMessageRequest(body_text=AGENT_REPLY),
        )
    assert queued.queued_for_delivery is True
    message_id = UUID(str(queued.message.id))
    outbound = await _queued_outbound(world, message_id)
    outcome = await send_outbound_message(
        world.factory,
        world.redis,
        EnvelopeCryptoService(get_settings()),
        outbound,
        adapter_registry=_im_registry(world.redis, fleet.transport),
    )
    assert outcome == "sent", outcome
    return message_id, outcome


async def _message_row(world: ImWorld, message_id: UUID) -> Any:
    async with tenant_session(world.factory, world.tenant_id) as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT direction, delivery_status, external_message_id, body_text "
                        "FROM messages WHERE tenant_id = :tid AND id = :mid"
                    ),
                    {"tid": world.tenant_id, "mid": message_id},
                )
            )
            .mappings()
            .one()
        )


async def _conversation_row(world: ImWorld, conversation_id: UUID) -> Any:
    async with tenant_session(world.factory, world.tenant_id) as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT channel_type, external_thread_id, customer_id, unread_count "
                        "FROM conversations WHERE tenant_id = :tid AND id = :cid"
                    ),
                    {"tid": world.tenant_id, "cid": conversation_id},
                )
            )
            .mappings()
            .one()
        )


async def _receipt_status(raw_event_id: str) -> str:
    async with _admin_engine() as engine, engine.begin() as connection:
        return str(
            await connection.scalar(
                text("SELECT processing_status FROM raw_webhook_events WHERE id = :id"),
                {"id": UUID(raw_event_id)},
            )
        )


# --------------------------------------------------------------------------------------
# A. Shopee buyer chat full round-trip
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shopee_buyer_im_full_round_trip(shopee_world: ImWorld) -> None:
    world = shopee_world
    shop_id = "7788990001"
    partner_key = "shopee-partner-key-im-e2e"
    buyer_id = "sg-buyer-e2e"
    inbound_message_id = "sg-msg-e2e-1"
    fleet = MarketplaceFleet()
    holder = [world.principal]

    async with tenant_session(world.factory, world.tenant_id) as session:
        await _seed_catalog(session, world.tenant_id)

    async with _running_app(holder, fleet.transport) as client:
        account_id = await _connect_shopee(client, shop_id=shop_id, partner_key=partner_key)
        world.streams.add(f"shopee:{shop_id}")

        payload = {
            "shop_id": shop_id,
            "from_id": buyer_id,
            "to_id": shop_id,
            "message_id": inbound_message_id,
            "message_type": "text",
            "content": {"text": BUYER_QUESTION},
            "created_timestamp": 1730000000,
        }
        body = _raw_body(payload)
        headers = {
            "content-type": "application/json",
            "x-shopee-shop-id": shop_id,
            "x-shopee-topic": "buyer_chat_message",
            "x-shopee-hmac-sha256": _hex_signature(partner_key, body),
        }

        accepted = await client.post("/api/v1/webhooks/im/shopee", content=body, headers=headers)
        assert accepted.status_code == 200, accepted.text
        receipt = accepted.json()
        assert receipt["status"] == "accepted"
        assert receipt["deduplicated"] is False
        [raw_event_id] = receipt["raw_event_ids"]
        world.raw_event_ids.add(raw_event_id)
        world.stream_keys.add(f"stream:webhooks:{ChannelType.SHOPEE.value}")

        # A tampered body must be rejected fail-closed, before any receipt exists.
        tampered = await client.post(
            "/api/v1/webhooks/im/shopee", content=body + b" ", headers=headers
        )
        assert tampered.status_code == 401, tampered.text
        assert tampered.json()["code"] == "ERR_WEBHOOK_SIGNATURE_INVALID"

        # An unknown platform never silently falls back onto the catch-all route.
        unknown = await client.post(
            "/api/v1/webhooks/im/alibaba-b2b", content=body, headers=headers
        )
        assert unknown.status_code == 404, unknown.text

        # The worker topology drains `stream:webhooks:shopee` and routes IM receipts into
        # process_raw_webhook_event instead of acking them as storefront order feeds.
        counts = await worker_main.drain_commerce_and_tracking_once(
            world.broker_factory, world.redis, runtime_session_factory=world.factory
        )
        assert counts["shopee"] >= 1

    assert await _receipt_status(raw_event_id) == "processed"

    async with tenant_session(world.factory, world.tenant_id) as session:
        conversation = (
            (
                await session.execute(
                    text(
                        "SELECT id, channel_type, external_thread_id, customer_id "
                        "FROM conversations WHERE tenant_id = :tid "
                        "AND channel_account_id = :aid"
                    ),
                    {"tid": world.tenant_id, "aid": UUID(account_id)},
                )
            )
            .mappings()
            .one()
        )
        assert conversation["channel_type"] == ChannelType.SHOPEE.value
        assert conversation["external_thread_id"] == buyer_id
        identity = (
            (
                await session.execute(
                    text(
                        "SELECT identity_type, identity_value FROM customer_identities "
                        "WHERE tenant_id = :tid AND customer_id = :cid"
                    ),
                    {"tid": world.tenant_id, "cid": conversation["customer_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert identity["identity_type"] == "marketplace_buyer_id"
        assert identity["identity_value"] == f"{ChannelType.SHOPEE.value}:{buyer_id}"
        inbound = (
            (
                await session.execute(
                    text(
                        "SELECT direction, body_text, external_message_id FROM messages "
                        "WHERE tenant_id = :tid AND conversation_id = :cid"
                    ),
                    {"tid": world.tenant_id, "cid": conversation["id"]},
                )
            )
            .mappings()
            .all()
        )
    assert [(row["direction"], row["external_message_id"]) for row in inbound] == [
        ("inbound", inbound_message_id)
    ]
    assert "LED-SPOT-12W" in str(inbound[0]["body_text"])

    # AI Customer Service grounding: the draft quotes only live ERP SQL figures.
    conversation_id = UUID(str(conversation["id"]))
    async with tenant_session(world.factory, world.tenant_id) as session:
        proposal = await AiProposalGeneratorService().generate_reply_proposal(
            session, world.tenant_id, conversation_id, llm_client=_OfflineLlm()
        )
    assert proposal.status == "pending"
    assert proposal.proposal_type == "reply_draft"
    assert proposal.source_tool_call_id is not None
    proposed = proposal.proposed_payload
    assert proposed["verified_tool_call_ids"]
    assert proposed["tool_evidence"][0]["tool_name"] == "get_live_sku_pricing_and_stock"
    assert proposed["tool_evidence"][0]["data"]["sku"] == PRODUCT_SKU
    assert proposed["tool_evidence"][0]["data"]["available_qty"] == ON_HAND_QTY - 20
    assert PRODUCT_SKU in proposed["body_text"]
    assert RETAIL_PRICE_USD in proposed["body_text"]

    # Human agent reply, dispatched through the signed Shopee seller-chat API.
    message_id, _outcome = await _send_agent_reply(world, conversation_id, fleet)
    sent_row = await _message_row(world, message_id)
    assert sent_row["direction"] == "outbound"
    assert sent_row["delivery_status"] == "sent"
    assert sent_row["external_message_id"] == "shopee-out-1"
    assert sent_row["body_text"] == AGENT_REPLY

    request = fleet.last("shopee")
    assert request.method == "POST"
    assert request.url.path == "/api/v2/sellerchat/send_message"
    assert request.url.host == "partner.shopeemobile.com"
    params = dict(request.url.params)
    assert set(params) >= {"partner_id", "timestamp", "sign", "access_token", "shop_id"}
    assert params["partner_id"] == "2010123"
    assert params["shop_id"] == shop_id
    assert params["access_token"] == f"shopee-access-token-{shop_id}"
    sent_payload = json.loads(request.content)
    assert sent_payload["to_id"] == buyer_id
    assert sent_payload["content"] == {"text": AGENT_REPLY}
    assert sent_payload["message_type"] == "text"
    # The HMAC the adapter signed must be recomputable from the wire request itself.
    assert params["sign"] == sign_shopee_request(
        partner_id="2010123",
        partner_key=partner_key,
        api_path="/api/v2/sellerchat/send_message",
        timestamp=int(params["timestamp"]),
        access_token=f"shopee-access-token-{shop_id}",
        shop_id=shop_id,
    )


# --------------------------------------------------------------------------------------
# B. Ozon, Mercado Libre and eBay: same ingress -> inbox -> outbound loop
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FLEET_CASES, ids=[case.slug for case in FLEET_CASES])
async def test_marketplace_im_ingress_and_outbound_round_trip(
    fleet_world: ImWorld, case: ImChannelCase
) -> None:
    world = fleet_world
    fleet = MarketplaceFleet()
    holder = [world.principal]
    run_id = uuid4().hex[:10]
    payload = json.loads(json.dumps(case.payload))
    event_id = f"{case.expected_event_id}-{run_id}"

    async with _running_app(holder, fleet.transport) as client:
        account_id = await _provision(world, case)

        if case.channel_type == ChannelType.OZON:
            payload["message_id"] = event_id
        elif case.channel_type == ChannelType.MERCADO_LIBRE:
            payload["message"]["id"] = event_id
        else:
            payload["notification"]["data"]["messageId"] = event_id

        body = _raw_body(payload)
        headers = {
            "content-type": "application/json",
            **case.headers,
            case.signature_header: _hex_signature(case.webhook_secret, body),
        }
        accepted = await client.post(
            f"/api/v1/webhooks/im/{case.slug}", content=body, headers=headers
        )
        assert accepted.status_code == 200, accepted.text
        [raw_event_id] = accepted.json()["raw_event_ids"]
        world.raw_event_ids.add(raw_event_id)
        world.stream_keys.add(f"stream:webhooks:{case.channel_type.value}")

        # Amazon SP-API / eBay / Alibaba International are consumed by the ingress worker
        # itself (worker_main does not drain those streams), so drain that path directly.
        if case.channel_type in INGRESS_ONLY_IM_CHANNELS:
            assert await _process_ingress_receipt(world, UUID(raw_event_id)) == "processed"
        else:
            handlers = worker_main.commerce_tracking_handlers(
                world.broker_factory,
                runtime_session_factory=world.factory,
                redis=world.redis,
            )
            assert await handlers[case.channel_type.value](raw_event_id) == 1

    assert await _receipt_status(raw_event_id) == "processed"

    async with tenant_session(world.factory, world.tenant_id) as session:
        conversation = (
            (
                await session.execute(
                    text(
                        "SELECT id, channel_type, external_thread_id FROM conversations "
                        "WHERE tenant_id = :tid AND channel_account_id = :aid"
                    ),
                    {"tid": world.tenant_id, "aid": account_id},
                )
            )
            .mappings()
            .one()
        )
        inbound = (
            (
                await session.execute(
                    text(
                        "SELECT direction, body_text FROM messages "
                        "WHERE tenant_id = :tid AND conversation_id = :cid"
                    ),
                    {"tid": world.tenant_id, "cid": conversation["id"]},
                )
            )
            .mappings()
            .all()
        )
    assert conversation["channel_type"] == case.channel_type.value
    assert conversation["external_thread_id"] == case.expected_thread
    assert [(row["direction"], str(row["body_text"])) for row in inbound] == [
        ("inbound", BUYER_QUESTION)
    ]

    conversation_id = UUID(str(conversation["id"]))
    message_id, _outcome = await _send_agent_reply(world, conversation_id, fleet)
    sent_row = await _message_row(world, message_id)
    assert sent_row["delivery_status"] == "sent"
    assert sent_row["external_message_id"] == case.expected_outbound_message_id

    request = fleet.last(case.fleet_platform)
    assert request.method == "POST"
    if case.channel_type == ChannelType.OZON:
        assert request.url.path == "/v1/chat/send/message"
        assert request.headers["client-id"] == "oz-client-e2e"
        assert request.headers["api-key"] == "ozon-api-key-e2e"
        assert json.loads(request.content) == {
            "chat_id": case.expected_thread,
            "text": AGENT_REPLY,
        }
    elif case.channel_type == ChannelType.MERCADO_LIBRE:
        assert request.url.path == (
            f"/messages/packs/{case.expected_thread}/sellers/{case.external_account_id}"
        )
        assert request.headers["authorization"] == "Bearer meli-access-token-e2e"
        assert json.loads(request.content)["text"] == AGENT_REPLY
    else:
        assert request.url.path == "/commerce/notification/v1/member_message"
        assert request.headers["authorization"] == "Bearer ebay-access-token-e2e"
        sent_payload = json.loads(request.content)
        assert sent_payload["messageText"] == AGENT_REPLY
        assert sent_payload["referenceId"] == case.expected_thread


# --------------------------------------------------------------------------------------
# C. Non-regression: an order push is not Inbox work
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shopee_order_webhook_is_not_routed_into_the_inbox(shopee_world: ImWorld) -> None:
    world = shopee_world
    shop_id = "7788990002"
    partner_key = "shopee-partner-key-order-e2e"
    fleet = MarketplaceFleet()
    holder = [world.principal]
    order_event_id = f"{EVENT_PREFIX}-order-{uuid4().hex[:10]}"
    world.raw_event_ids.add(order_event_id)

    async with _running_app(holder, fleet.transport) as client:
        await _connect_shopee(client, shop_id=shop_id, partner_key=partner_key)
        world.streams.add(f"shopee:{shop_id}")
        world.stream_keys.add(f"stream:webhooks:{ChannelType.SHOPEE.value}")

        order_payload = {
            "ordersn": "241123ABCDEF",
            "status": "READY_TO_SHIP",
            "shop_id": shop_id,
            "buyer_user_id": "sg-buyer-order",
            "total_amount": "42.50",
        }
        body = _raw_body(order_payload)
        headers = {
            "content-type": "application/json",
            "x-shopee-shop-id": shop_id,
            "x-shopee-topic": "orders/create",
            "x-shopee-event-id": order_event_id,
            "x-shopee-hmac-sha256": _hex_signature(partner_key, body),
        }
        accepted = await client.post("/api/v1/webhooks/shopee", content=body, headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["code"] == 200

    async with _admin_engine() as engine, engine.begin() as connection:
        raw_event_id = str(
            await connection.scalar(
                text("SELECT id FROM raw_webhook_events WHERE external_event_id = :eid"),
                {"eid": order_event_id},
            )
        )
    assert raw_event_id and raw_event_id != "None"

    handlers = worker_main.commerce_tracking_handlers(
        world.broker_factory, runtime_session_factory=world.factory, redis=world.redis
    )
    assert await handlers["shopee"](raw_event_id) == 1
    assert await _receipt_status(raw_event_id) == "processed"

    async with tenant_session(world.factory, world.tenant_id) as session:
        conversations = await session.scalar(
            text("SELECT COUNT(*) FROM conversations WHERE tenant_id = :tid"),
            {"tid": world.tenant_id},
        )
        messages = await session.scalar(
            text("SELECT COUNT(*) FROM messages WHERE tenant_id = :tid"),
            {"tid": world.tenant_id},
        )
        customers = await session.scalar(
            text("SELECT COUNT(*) FROM customers WHERE tenant_id = :tid"),
            {"tid": world.tenant_id},
        )
    assert (conversations, messages, customers) == (0, 0, 0)


# --------------------------------------------------------------------------------------
# D. Topology guards (no PG/Redis needed)
# --------------------------------------------------------------------------------------


def test_ingress_worker_owns_only_the_three_unclaimed_marketplace_streams() -> None:
    """One Redis stream must never get two consumer groups."""
    worker_channels = {channel.value for channel in _WORKER_CHANNELS}
    for channel_type in INGRESS_ONLY_IM_CHANNELS:
        assert channel_type.value in worker_channels

    commerce_channels = set(worker_main.COMMERCE_TRACKING_CHANNELS)
    for channel_type in MARKETPLACE_IM_CHANNELS:
        if channel_type in INGRESS_ONLY_IM_CHANNELS:
            assert channel_type.value not in commerce_channels
            continue
        # Already drained by worker_main: adding it here would double-consume it.
        assert channel_type.value in commerce_channels
        assert channel_type.value not in worker_channels


def test_default_adapter_registry_resolves_every_marketplace_im_channel() -> None:
    registry = build_default_adapter_registry(None)
    built = build_marketplace_im_adapters()
    assert set(built) == set(MARKETPLACE_IM_CHANNELS)
    for channel_type, adapter in built.items():
        resolved = registry.get(channel_type.value)
        assert resolved is not None, channel_type
        assert resolved.channel_types == adapter.channel_types
        assert registry.get(channel_type.value.replace("_", "-")) is resolved
    assert registry.get("alibaba-intl") is registry.get(ChannelType.ALIBABA_EMAIL.value)


def test_marketplace_im_platform_slug_map_covers_both_spellings() -> None:
    from erp_core.channels.webhook_router import _MARKETPLACE_IM_PLATFORM_ALIASES

    expected: dict[str, ChannelType] = {
        "shopee": ChannelType.SHOPEE,
        "lazada": ChannelType.LAZADA,
        "temu": ChannelType.TEMU,
        "shein": ChannelType.SHEIN,
        "aliexpress": ChannelType.ALIEXPRESS,
        "ozon": ChannelType.OZON,
        "walmart": ChannelType.WALMART,
        "mercado_libre": ChannelType.MERCADO_LIBRE,
        "mercado-libre": ChannelType.MERCADO_LIBRE,
        "tiktok_shop": ChannelType.TIKTOK_SHOP,
        "tiktok-shop": ChannelType.TIKTOK_SHOP,
        "ebay": ChannelType.EBAY,
        "amazon_sp": ChannelType.AMAZON_SP,
        "amazon-sp": ChannelType.AMAZON_SP,
        "alibaba_email": ChannelType.ALIBABA_EMAIL,
        "alibaba-intl": ChannelType.ALIBABA_EMAIL,
        "alibaba-email": ChannelType.ALIBABA_EMAIL,
    }
    for alias, channel_type in expected.items():
        assert _MARKETPLACE_IM_PLATFORM_ALIASES[alias] is channel_type
    assert set(MARKETPLACE_IM_CHANNELS) == {
        channel_type
        for alias, channel_type in _MARKETPLACE_IM_PLATFORM_ALIASES.items()
        if alias in {channel.value for channel in MARKETPLACE_IM_CHANNELS}
    }
