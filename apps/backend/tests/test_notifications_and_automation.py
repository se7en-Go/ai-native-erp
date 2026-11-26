"""Track 1: Feishu/DingTalk/WeCom notifications and event-driven automation rules.

Everything here runs against real PostgreSQL 17 with FORCE RLS, the real routers and
real tenant-scoped SQL. HTTP delivery uses an injected `httpx.MockTransport`, so no test
touches the public internet while still asserting the exact signed payloads.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import erp_core.tenancy.models  # noqa: F401  (FK targets for the notification mappers)
from erp_core.ai_engine.automation_service import DEFAULT_RULE_TEMPLATES, AutomationService
from erp_core.ai_engine.notification_router import router as notification_router
from erp_core.ai_engine.notification_schemas import NotificationChannelCreate
from erp_core.ai_engine.notification_service import (
    ALLOW_LOCAL_WEBHOOK_URLS_ENV,
    MAX_DELIVERY_ATTEMPTS,
    WEBHOOK_SECRET_CRED_TYPE,
    NotificationService,
    build_webhook_request,
    channel_accepts_priority,
    dingtalk_signature,
    feishu_signature,
    generic_webhook_signature,
    mask_webhook_url,
    validate_webhook_url,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/ai"
SECRET = "track1-signing-secret"
FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef123456"
DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=abcdef123456"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456"


# --------------------------------------------------------------------------------------
# Deterministic HTTP transport
# --------------------------------------------------------------------------------------


@dataclass
class RecordedCall:
    url: str
    headers: dict[str, str]
    body: bytes


@dataclass
class StubTransport:
    """Records every POST and answers with a scripted status sequence."""

    statuses: list[int] = field(default_factory=lambda: [200])
    calls: list[RecordedCall] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(
                RecordedCall(
                    url=str(request.url),
                    headers={key.lower(): value for key, value in request.headers.items()},
                    body=request.content,
                )
            )
            index = min(len(self.calls) - 1, len(self.statuses) - 1)
            return httpx.Response(self.statuses[index], text="stub")

        return httpx.MockTransport(handler)


def service_with(
    stub: StubTransport,
    *,
    crypto: EnvelopeCryptoService | None = None,
    allow_local: bool = True,
) -> NotificationService:
    return NotificationService(
        crypto=crypto or EnvelopeCryptoService(get_settings()),
        transport=stub.transport(),
        allow_local_urls=allow_local,
    )


# --------------------------------------------------------------------------------------
# Workspace fixture
# --------------------------------------------------------------------------------------


@dataclass
class Workspace:
    tenant_a: UUID
    tenant_b: UUID
    owner_a: UUID
    manager_a: UUID
    agent_a: UUID
    owner_b: UUID
    variant_id: UUID
    low_stock_sku: str
    negative_order_no: str
    stalled_tracking: str
    cold_quote_no: str
    factory: Factory

    def principal(
        self, user_id: UUID, role: UserRole, tenant_id: UUID | None = None
    ) -> CurrentPrincipal:
        return CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id or self.tenant_a,
            role=role,
            email=f"{user_id.hex}@track1.example.com",
        )


async def _insert_user(session: AsyncSession, user_id: UUID, label: str) -> None:
    await session.execute(
        text(
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:uid, :email, :name, 'x')"
        ),
        {"uid": user_id, "email": f"{user_id.hex}@track1.example.com", "name": label},
    )


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[Workspace]:
    tenant_a, tenant_b = uuid4(), uuid4()
    owner_a, manager_a, agent_a, owner_b = (uuid4() for _ in range(4))
    for tid, slug in ((tenant_a, f"track1-{tenant_a.hex}"), (tenant_b, f"track1-{tenant_b.hex}")):
        async with tenant_session(runtime_session_factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Track 1')"),
                {"id": tid, "slug": slug},
            )
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        for uid, role, label in (
            (owner_a, UserRole.OWNER, "Owner A"),
            (manager_a, UserRole.MANAGER, "Manager A"),
            (agent_a, UserRole.AGENT, "Agent A"),
        ):
            await _insert_user(session, uid, label)
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant_a, "uid": uid, "role": role.value},
            )
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await _insert_user(session, owner_b, "Owner B")
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": tenant_b, "uid": owner_b},
        )

    product_id, variant_id = uuid4(), uuid4()
    customer_id, opportunity_id = uuid4(), uuid4()
    negative_order_id, velocity_order_id = uuid4(), uuid4()
    quote_id, shipment_id = uuid4(), uuid4()
    stale = datetime.now(UTC) - timedelta(hours=100)

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:id, :tid, 'SPU-TRACK1', 'Lighting')"
            ),
            {"id": product_id, "tid": tenant_a},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants ("
                " id, tenant_id, product_id, sku, attributes, cost_price_base, base_currency,"
                " retail_price_usd, weight_grams, lead_time_days)"
                " VALUES (:id, :tid, :pid, :sku, CAST(:attrs AS jsonb), 10.0000, 'CNY',"
                " 29.00, 500, 7)"
            ),
            {
                "id": variant_id,
                "tid": tenant_a,
                "pid": product_id,
                "sku": "LED-TRACK1-01",
                "attrs": '{"safety_stock_qty": 100}',
            },
        )
        # Available 5 < safety stock 100 -> low-stock match.
        await session.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id,"
                " on_hand_qty, reserved_qty) VALUES (:tid, :wh, :vid, 5, 0)"
            ),
            {"tid": tenant_a, "wh": uuid4(), "vid": variant_id},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name) "
                "VALUES (:id, :tid, 'Track 1 Buyer')"
            ),
            {"id": customer_id, "tid": tenant_a},
        )
        # Velocity order: 30 units in the last 30 days -> 1.0/day.
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency,"
                " fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status,"
                " payment_status, channel_metadata)"
                " VALUES (:id, :tid, :no, 'b2c_retail', :cid, 'USD', 1.000000, 300.00, 300.00,"
                " 'shipped', 'paid', '{}'::jsonb)"
            ),
            {"id": velocity_order_id, "tid": tenant_a, "no": "SO-TRACK1-0001", "cid": customer_id},
        )
        await session.execute(
            text(
                "INSERT INTO order_items (tenant_id, order_id, variant_id, sku_snapshot,"
                " quantity, unit_price, unit_cost_base_snapshot, line_total)"
                " VALUES (:tid, :oid, :vid, 'LED-TRACK1-01', 30, 10.0000, 5.0000, 300.00)"
            ),
            {"tid": tenant_a, "oid": velocity_order_id, "vid": variant_id},
        )
        # Negative margin order: gross 100 vs COGS 150 -> -50 base.
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency,"
                " fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status,"
                " payment_status, channel_metadata, created_at)"
                " VALUES (:id, :tid, :no, 'b2b_wholesale', :cid, 'USD', 1.000000, 100.00, 100.00,"
                " 'in_production', 'paid', '{}'::jsonb, :created)"
            ),
            {
                "id": negative_order_id,
                "tid": tenant_a,
                "no": "SO-TRACK1-0002",
                "cid": customer_id,
                "created": datetime.now(UTC) - timedelta(days=1),
            },
        )
        await session.execute(
            text(
                "INSERT INTO order_profit_snapshots (order_id, tenant_id, base_currency,"
                " gross_revenue_base, cogs_base)"
                " VALUES (:oid, :tid, 'USD', 100.00, 150.00)"
            ),
            {"oid": negative_order_id, "tid": tenant_a},
        )
        await session.execute(
            text(
                "INSERT INTO shipments (id, tenant_id, order_id, tracking_provider,"
                " tracking_number, standardized_status, created_at)"
                " VALUES (:id, :tid, :oid, '17track', 'YT-TRACK1-0001', 'in_transit', :created)"
            ),
            {"id": shipment_id, "tid": tenant_a, "oid": negative_order_id, "created": stale},
        )
        await session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, customer_id, title, stage,"
                " owner_user_id)"
                " VALUES (:id, :tid, :cid, 'Track 1 opportunity', 'quoting', :owner)"
            ),
            {"id": opportunity_id, "tid": tenant_a, "cid": customer_id, "owner": owner_a},
        )
        await session.execute(
            text(
                "INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, version,"
                " incoterm, payment_terms, fx_rate_snapshot, fx_locked_until, total_amount,"
                " status, created_at)"
                " VALUES (:id, :tid, :oid, 'PI-TRACK1-0001', 1, 'FOB', 'TT 30 days',"
                " 7.200000, :fx_locked, 1000.00, 'sent', :created)"
            ),
            {
                "id": quote_id,
                "tid": tenant_a,
                "oid": opportunity_id,
                "fx_locked": datetime.now(UTC) + timedelta(days=30),
                "created": stale,
            },
        )

    yield Workspace(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        owner_a=owner_a,
        manager_a=manager_a,
        agent_a=agent_a,
        owner_b=owner_b,
        variant_id=variant_id,
        low_stock_sku="LED-TRACK1-01",
        negative_order_no="SO-TRACK1-0002",
        stalled_tracking="YT-TRACK1-0001",
        cold_quote_no="PI-TRACK1-0001",
        factory=runtime_session_factory,
    )

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        for statement in (
            "DELETE FROM notification_deliveries",
            "DELETE FROM notification_channels",
            "DELETE FROM automation_rules",
            "DELETE FROM ai_proposals",
            "DELETE FROM quotes",
            "DELETE FROM opportunities",
            "DELETE FROM shipments",
            "DELETE FROM order_profit_snapshots",
            "DELETE FROM order_items",
            "DELETE FROM orders",
            "DELETE FROM inventory_balances",
            "DELETE FROM product_variants",
            "DELETE FROM products",
            "DELETE FROM customers",
        ):
            await session.execute(text(statement))


# --------------------------------------------------------------------------------------
# Signature + payload helpers
# --------------------------------------------------------------------------------------


def test_feishu_signature_matches_documented_hmac() -> None:
    timestamp = "1700000000"
    expected = base64.b64encode(
        hmac.new(f"{timestamp}\n{SECRET}".encode(), b"", hashlib.sha256).digest()
    ).decode()
    assert feishu_signature(SECRET, timestamp) == expected

    request = build_webhook_request(
        provider="feishu",
        webhook_url=FEISHU_URL,
        title="补货提醒",
        body="3 个 SKU 低于安全库存",
        deep_link="/wms",
        secret=SECRET,
        now=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
    )
    assert request.payload["msg_type"] == "text"
    assert request.payload["timestamp"] == timestamp
    assert request.payload["sign"] == expected
    assert "补货提醒" in request.payload["content"]["text"]
    assert "/wms" in request.payload["content"]["text"]


def test_dingtalk_signature_is_url_encoded_query_param() -> None:
    timestamp_ms = 1700000000123
    raw = base64.b64encode(
        hmac.new(
            SECRET.encode(), f"{timestamp_ms}\n{SECRET}".encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert dingtalk_signature(SECRET, timestamp_ms) == quote_plus(raw)

    request = build_webhook_request(
        provider="dingtalk",
        webhook_url=DINGTALK_URL,
        title="负毛利拦截",
        body="SO-1 毛利为负",
        secret=SECRET,
        now=datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC),
    )
    assert "access_token=abcdef123456" in request.url
    assert f"timestamp={timestamp_ms}" in request.url
    assert "sign=" in request.url
    assert request.payload["msgtype"] == "markdown"
    assert request.payload["markdown"]["title"] == "负毛利拦截"


def test_wecom_payload_and_generic_webhook_signature() -> None:
    wecom = build_webhook_request(
        provider="wecom",
        webhook_url=WECOM_URL,
        title="每日巡检简报",
        body="健康分 88",
        secret=SECRET,
    )
    assert wecom.url == WECOM_URL
    assert wecom.payload["msgtype"] == "markdown"
    assert "每日巡检简报" in wecom.payload["markdown"]["content"]
    # WeCom bot signing is not part of the inbound payload contract we implement.
    assert "sign" not in wecom.payload

    generic = build_webhook_request(
        provider="generic_webhook",
        webhook_url="https://hooks.example.com/erp",
        title="订单告警",
        body="SO-1",
        secret=SECRET,
        event_type="automation.negative_margin_hold",
        priority="P0",
    )
    assert generic.payload["event_type"] == "automation.negative_margin_hold"
    assert generic.payload["priority"] == "P0"
    assert generic.headers["X-ERP-Signature"] == generic_webhook_signature(
        SECRET, generic.body
    )


def test_priority_filter_matrix() -> None:
    assert channel_accepts_priority("P0", "P0") is True
    assert channel_accepts_priority("P0", "P1") is False
    assert channel_accepts_priority("P0", "P2") is False
    assert channel_accepts_priority("P1", "P0") is True
    assert channel_accepts_priority("P1", "P1") is True
    assert channel_accepts_priority("P1", "P2") is False
    assert channel_accepts_priority("P2", "P0") is True
    assert channel_accepts_priority("P2", "P1") is True
    assert channel_accepts_priority("P2", "P2") is True


def test_webhook_url_masking_never_leaks_the_bot_token() -> None:
    preview = mask_webhook_url(FEISHU_URL)
    assert preview == "https://open.feishu.cn/***3456"
    assert "abcdef123456" not in preview


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://127.0.0.1:8080/hook",
        "http://localhost/hook",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.9/hook",
        "http://[::1]/hook",
        "http://metadata.google.internal/hook",
        "file:///etc/passwd",
        "http://user:pass@open.feishu.cn/hook",
    ],
)
def test_ssrf_guard_blocks_private_and_non_http_targets(unsafe_url: str) -> None:
    with pytest.raises(AppError) as exc_info:
        validate_webhook_url(unsafe_url, allow_local=False)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code in {
        "ERR_UNSAFE_WEBHOOK_URL",
        "ERR_WEBHOOK_URL_CREDENTIALS",
    }


def test_ssrf_guard_allows_explicit_local_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOW_LOCAL_WEBHOOK_URLS_ENV, raising=False)
    assert validate_webhook_url("http://127.0.0.1:8099/hook", allow_local=True) == (
        "http://127.0.0.1:8099/hook"
    )
    monkeypatch.setenv(ALLOW_LOCAL_WEBHOOK_URLS_ENV, "1")
    assert validate_webhook_url("http://127.0.0.1:8099/hook") == "http://127.0.0.1:8099/hook"


# --------------------------------------------------------------------------------------
# RLS isolation, encrypted secrets and sending
# --------------------------------------------------------------------------------------


async def test_pg17_force_rls_isolates_notification_tables(workspace: Workspace) -> None:
    stub = StubTransport()
    service = service_with(stub)
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        channel = await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Tenant A Feishu",
                provider="feishu",
                webhook_url=FEISHU_URL,
                secret=SECRET,
                min_priority="P1",
            ),
            actor_id=workspace.owner_a,
        )

    # Tenant B cannot see, update or delete tenant A's channel.
    async with tenant_session(workspace.factory, workspace.tenant_b) as session:
        assert await service.list_channels(session, workspace.tenant_b) == []
        with pytest.raises(AppError) as exc_info:
            await service._load_channel(session, workspace.tenant_b, channel.id)
        assert exc_info.value.status_code == 404

    # A connection with no tenant variable set is fail-closed even for app_runtime.
    async with system_session(workspace.factory) as session:
        visible = await session.scalar(text("SELECT COUNT(*) FROM notification_channels"))
        deliveries = await session.scalar(text("SELECT COUNT(*) FROM notification_deliveries"))
        assert int(visible or 0) == 0
        assert int(deliveries or 0) == 0

    # Cross-tenant write must be rejected by the FORCE RLS WITH CHECK clause.
    with pytest.raises(Exception) as denied:
        async with tenant_session(workspace.factory, workspace.tenant_b) as session:
            await session.execute(
                text(
                    "INSERT INTO notification_channels (tenant_id, name, provider, webhook_url)"
                    " VALUES (:tid, 'Leak', 'feishu', :url)"
                ),
                {"tid": workspace.tenant_a, "url": FEISHU_URL},
            )
    assert "row-level security" in str(denied.value).lower()


async def test_signing_secret_is_encrypted_at_rest(workspace: Workspace) -> None:
    stub = StubTransport()
    crypto = EnvelopeCryptoService(get_settings())
    service = service_with(stub, crypto=crypto)
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        created = await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Encrypted Feishu",
                provider="feishu",
                webhook_url=FEISHU_URL,
                secret=SECRET,
            ),
            actor_id=workspace.owner_a,
        )
        assert created.has_signing_secret is True
        assert SECRET not in created.model_dump_json()

        row = (
            await session.execute(
                text(
                    "SELECT secret_ciphertext, secret_nonce, secret_auth_tag,"
                    " secret_key_version, webhook_url FROM notification_channels WHERE id = :id"
                ),
                {"id": created.id},
            )
        ).one()
        ciphertext, nonce, auth_tag, key_version, webhook_url = row
        assert bytes(ciphertext) != SECRET.encode()
        assert SECRET.encode() not in bytes(ciphertext)
        assert len(bytes(nonce)) == 12
        assert len(bytes(auth_tag)) == 16
        assert int(key_version) >= 1
        # The URL is stored in full for delivery but the read model masks it.
        assert webhook_url == FEISHU_URL
        assert "abcdef123456" not in created.webhook_url_preview

        assert (
            await crypto.decrypt_secret(
                session,
                workspace.tenant_a,
                created.id,
                WEBHOOK_SECRET_CRED_TYPE,
                bytes(ciphertext),
                bytes(nonce),
                bytes(auth_tag),
                int(key_version),
            )
            == SECRET
        )


async def test_priority_filtering_only_queues_admitted_channels(workspace: Workspace) -> None:
    stub = StubTransport()
    service = service_with(stub)
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        p0 = await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="P0 only",
                provider="feishu",
                webhook_url=FEISHU_URL,
                min_priority="P0",
            ),
            actor_id=workspace.owner_a,
        )
        p1 = await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="P1 and above",
                provider="wecom",
                webhook_url=WECOM_URL,
                min_priority="P1",
            ),
            actor_id=workspace.owner_a,
        )
        p2 = await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Everything",
                provider="generic_webhook",
                webhook_url="https://hooks.example.com/erp",
                min_priority="P2",
            ),
            actor_id=workspace.owner_a,
        )

        p2_only = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.daily_patrol_brief",
            title="简报",
            body="P2",
            priority="P2",
        )
        assert {str(item.channel_id) for item in p2_only} == {str(p2.id)}

        p1_rows = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.low_doc_replenishment",
            title="补货",
            body="P1",
            priority="P1",
        )
        assert {str(item.channel_id) for item in p1_rows} == {str(p1.id), str(p2.id)}

        p0_rows = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.negative_margin_hold",
            title="负毛利",
            body="P0",
            priority="P0",
        )
        assert {str(item.channel_id) for item in p0_rows} == {str(p0.id), str(p1.id), str(p2.id)}
        assert [item.status for item in p0_rows] == ["pending", "pending", "pending"]


async def test_retry_backoff_then_failure_and_successful_dispatch(workspace: Workspace) -> None:
    stub = StubTransport(statuses=[500])
    service = service_with(stub)
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Failing Feishu",
                provider="feishu",
                webhook_url="http://127.0.0.1:9999/hook",
                secret=SECRET,
                min_priority="P0",
            ),
            actor_id=workspace.owner_a,
        )
        deliveries = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.negative_margin_hold",
            title="负毛利",
            body="P0",
            priority="P0",
        )
        assert len(deliveries) == 1
        delivery = deliveries[0]
        await service.dispatch_delivery(session, delivery)
        assert delivery.status == "retrying"
        assert delivery.attempts == 1
        assert delivery.http_status == 500
        assert delivery.next_retry_at is not None
        delta = delivery.next_retry_at - datetime.now(UTC)
        assert timedelta(minutes=9) < delta <= timedelta(minutes=10, seconds=5)

        # Backoff elapses -> a due-retry sweep drives attempt 2 and then attempt 3.
        for expected_attempt in (2, MAX_DELIVERY_ATTEMPTS):
            await session.execute(
                text(
                    "UPDATE notification_deliveries"
                    " SET next_retry_at = now() - interval '1 minute' WHERE id = :id"
                ),
                {"id": delivery.id},
            )
            sweep = await service.retry_due_deliveries(session, workspace.tenant_a)
            assert sweep.attempted == 1
            await session.refresh(delivery)
            assert delivery.attempts == expected_attempt
        assert delivery.status == "failed"
        assert delivery.next_retry_at is None
        assert len(stub.calls) == MAX_DELIVERY_ATTEMPTS

        # A terminal failure is never retried again.
        sweep = await service.retry_due_deliveries(session, workspace.tenant_a)
        assert sweep.attempted == 0
        assert len(stub.calls) == MAX_DELIVERY_ATTEMPTS


async def test_dispatch_rejects_hostname_that_resolves_to_private_ip(workspace: Workspace) -> None:
    stub = StubTransport()

    async def private_dns(_host: str) -> list[str]:
        return ["10.1.2.3"]

    service = NotificationService(
        crypto=EnvelopeCryptoService(get_settings()),
        transport=stub.transport(),
        allow_local_urls=False,
        dns_resolver=private_dns,
    )
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Rebinding webhook",
                provider="generic_webhook",
                webhook_url="https://rebind.example.com/erp",
            ),
            actor_id=workspace.owner_a,
        )
        deliveries = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.low_doc_replenishment",
            title="补货",
            body="3 SKU",
            priority="P1",
        )
        await service.dispatch_delivery(session, deliveries[0])
        assert deliveries[0].status == "failed"
        assert deliveries[0].next_retry_at is None
        assert "non-public address" in (deliveries[0].error_message or "")
    # The SSRF guard rejected the target before any HTTP request left the process.
    assert stub.calls == []


# --------------------------------------------------------------------------------------
# Router: RBAC, CRUD, delivery log, retry sweep, test ping
# --------------------------------------------------------------------------------------


def _app(factory: Factory, principal: CurrentPrincipal, service: NotificationService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = service.crypto()
    app.state.notification_service = service
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(notification_router)
    return app


def client_for(
    factory: Factory, principal: CurrentPrincipal, service: NotificationService
) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_app(factory, principal, service)), base_url="http://t"
    )


async def test_router_rbac_blocks_agent_but_allows_reads(workspace: Workspace) -> None:
    stub = StubTransport()
    service = service_with(stub)
    agent_client = client_for(
        workspace.factory, workspace.principal(workspace.agent_a, UserRole.AGENT), service
    )
    async with agent_client:
        read = await agent_client.get(f"{BASE}/notifications/channels")
        assert read.status_code == 200
        created = await agent_client.post(
            f"{BASE}/notifications/channels",
            json={
                "name": "Agent attempt",
                "provider": "feishu",
                "webhook_url": FEISHU_URL,
            },
        )
        assert created.status_code == 403
        assert created.json()["code"] == "ERR_INSUFFICIENT_ROLE"
        evaluated = await agent_client.post(
            f"{BASE}/automation-rules/evaluate", json={"dry_run": True}
        )
        assert evaluated.status_code == 403
        deliveries = await agent_client.get(f"{BASE}/notifications/deliveries")
        assert deliveries.status_code == 200
        assert deliveries.json()["items"] == []

    owner_client = client_for(
        workspace.factory, workspace.principal(workspace.owner_a, UserRole.OWNER), service
    )
    async with owner_client:
        created = await owner_client.post(
            f"{BASE}/notifications/channels",
            json={
                "name": "Owner Feishu",
                "provider": "feishu",
                "webhook_url": FEISHU_URL,
                "secret": SECRET,
                "min_priority": "P1",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["has_signing_secret"] is True
        assert "abcdef123456" not in body["webhook_url_preview"]
        assert "secret" not in body

        test_ping = await owner_client.post(
            f"{BASE}/notifications/channels/{body['id']}/test"
        )
        assert test_ping.status_code == 200, test_ping.text
        assert test_ping.json()["ok"] is True
        assert len(stub.calls) == 1
        assert stub.calls[0].url == FEISHU_URL
        assert "sign" in stub.calls[0].body.decode()

        listed = await owner_client.get(f"{BASE}/notifications/channels")
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()] == ["Owner Feishu"]

        patched = await owner_client.patch(
            f"{BASE}/notifications/channels/{body['id']}",
            json={"min_priority": "P0", "is_enabled": False},
        )
        assert patched.status_code == 200
        assert patched.json()["min_priority"] == "P0"
        assert patched.json()["is_enabled"] is False

        deleted = await owner_client.delete(f"{BASE}/notifications/channels/{body['id']}")
        assert deleted.status_code == 204
        assert (await owner_client.get(f"{BASE}/notifications/channels")).json() == []

        # Deleting an already-deleted channel is a clean 404, not a server error.
        again = await owner_client.delete(f"{BASE}/notifications/channels/{body['id']}")
        assert again.status_code == 404


async def test_router_rejects_private_webhook_url(workspace: Workspace) -> None:
    stub = StubTransport()
    # No local opt-in: the router service must reject a loopback URL outright.
    service = NotificationService(
        crypto=EnvelopeCryptoService(get_settings()),
        transport=stub.transport(),
        allow_local_urls=False,
    )
    client = client_for(
        workspace.factory, workspace.principal(workspace.owner_a, UserRole.OWNER), service
    )
    async with client:
        response = await client.post(
            f"{BASE}/notifications/channels",
            json={
                "name": "Loopback",
                "provider": "generic_webhook",
                "webhook_url": "http://127.0.0.1:9001/hook",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "ERR_UNSAFE_WEBHOOK_URL"


async def test_router_delivery_log_and_retry_sweep(workspace: Workspace) -> None:
    stub = StubTransport(statuses=[503, 200])
    service = service_with(stub)
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Delivery log channel",
                provider="wecom",
                webhook_url="http://127.0.0.1:9998/hook",
                min_priority="P2",
            ),
            actor_id=workspace.owner_a,
        )
        deliveries = await service.queue_delivery(
            session,
            workspace.tenant_a,
            event_type="automation.stalled_shipment_soothe",
            title="停滞货件",
            body="1 票",
            priority="P2",
        )
        await service.dispatch_delivery(session, deliveries[0])
        assert deliveries[0].status == "retrying"

    client = client_for(
        workspace.factory, workspace.principal(workspace.manager_a, UserRole.MANAGER), service
    )
    async with client:
        log = await client.get(f"{BASE}/notifications/deliveries?status=retrying")
        assert log.status_code == 200, log.text
        payload = log.json()
        assert payload["total"] == 1
        assert payload["items"][0]["channel_name"] == "Delivery log channel"
        assert payload["items"][0]["http_status"] == 503
        assert payload["items"][0]["attempts"] == 1

        async with tenant_session(workspace.factory, workspace.tenant_a) as session:
            await session.execute(
                text(
                    "UPDATE notification_deliveries"
                    " SET next_retry_at = now() - interval '1 minute'"
                )
            )

        sweep = await client.post(f"{BASE}/notifications/deliveries/retry-due")
        assert sweep.status_code == 200, sweep.text
        assert sweep.json() == {"attempted": 1, "sent": 1, "retrying": 0, "failed": 0}

        log_after = await client.get(f"{BASE}/notifications/deliveries?status=sent")
        assert log_after.json()["total"] == 1
        assert log_after.json()["items"][0]["sent_at"] is not None


# --------------------------------------------------------------------------------------
# Automation rules: dry run vs execute across all five templates
# --------------------------------------------------------------------------------------


async def _automation_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            text(
                "SELECT 'deliveries', COUNT(*) FROM notification_deliveries"
                " UNION ALL SELECT 'proposals', COUNT(*) FROM ai_proposals WHERE"
                " proposed_payload ->> 'rule' IS NOT NULL"
                " UNION ALL SELECT 'rules', COUNT(*) FROM automation_rules"
            )
        )
    ).all()
    return {str(key): int(count) for key, count in rows}


async def test_automation_dry_run_is_read_only_and_accurate(workspace: Workspace) -> None:
    stub = StubTransport()
    service = service_with(stub)
    automation = AutomationService()
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        before = await _automation_counts(session)
        report = await automation.evaluate_rules(
            session,
            workspace.tenant_a,
            dry_run=True,
            notification_service=service,
        )
        after = await _automation_counts(session)

    assert report.dry_run is True
    assert report.seeded_defaults is False
    by_code = {item.template_code: item for item in report.rules}
    assert set(by_code) == set(DEFAULT_RULE_TEMPLATES)
    assert by_code["low_doc_replenishment"].matched_count == 1
    assert by_code["low_doc_replenishment"].items_preview[0]["sku"] == workspace.low_stock_sku
    assert by_code["negative_margin_hold"].matched_count == 1
    assert by_code["negative_margin_hold"].items_preview[0]["order_no"] == (
        workspace.negative_order_no
    )
    assert by_code["stalled_shipment_soothe"].matched_count == 1
    assert by_code["stalled_shipment_soothe"].items_preview[0]["tracking_number"] == (
        workspace.stalled_tracking
    )
    assert by_code["cold_b2b_quote_followup"].matched_count == 1
    assert by_code["cold_b2b_quote_followup"].items_preview[0]["quote_no"] == (
        workspace.cold_quote_no
    )
    assert by_code["daily_patrol_brief"].matched_count >= 1
    assert report.total_matched == sum(item.matched_count for item in report.rules)
    assert report.total_proposals_created == 0
    assert report.total_notifications_queued == 0
    assert all("dry_run" in (item.note or "") for item in report.rules)
    # Nothing was written: no deliveries, no proposals, no rule rows.
    assert before == after == {"deliveries": 0, "proposals": 0, "rules": 0}
    assert stub.calls == []


async def test_automation_execute_seeds_rules_creates_l1_proposals_and_notifications(
    workspace: Workspace,
) -> None:
    stub = StubTransport()
    service = service_with(stub)
    automation = AutomationService()
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        await service.create_channel(
            session,
            workspace.tenant_a,
            NotificationChannelCreate(
                name="Ops Feishu",
                provider="feishu",
                webhook_url="http://127.0.0.1:9997/hook",
                min_priority="P2",
            ),
            actor_id=workspace.owner_a,
        )
        first = await automation.evaluate_rules(
            session,
            workspace.tenant_a,
            notification_service=service,
        )
        assert first.dry_run is False
        assert first.seeded_defaults is True
        by_code = {item.template_code: item for item in first.rules}
        assert by_code["low_doc_replenishment"].proposals_created == 1
        assert by_code["stalled_shipment_soothe"].proposals_created == 1
        assert by_code["cold_b2b_quote_followup"].proposals_created == 1
        assert by_code["negative_margin_hold"].proposals_created == 0
        assert by_code["daily_patrol_brief"].proposals_created == 0
        # One aggregated notification per rule, all admitted by the P2 channel.
        assert first.total_notifications_queued == 5
        assert all(item.notifications_queued == 1 for item in first.rules)

        rules = await automation.list_rules(session, workspace.tenant_a)
        assert len(rules) == 5
        assert all(rule.last_run_at is not None for rule in rules)
        assert all(rule.last_triggered_count >= 0 for rule in rules)

        proposals = (
            await session.execute(
                text(
                    "SELECT capability, proposal_type, status, proposed_payload"
                    " FROM ai_proposals WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": workspace.tenant_a},
            )
        ).all()
        assert len(proposals) == 3
        rules_seen = {row[3]["rule"] for row in proposals}
        assert rules_seen == {
            "low_doc_replenishment",
            "stalled_shipment_soothe",
            "cold_b2b_quote_followup",
        }
        for capability, proposal_type, status, payload in proposals:
            assert status == "pending"  # L1 proposal, never auto-approved
            assert capability in {"order_logistics", "sales_quoting"}
            assert proposal_type == "follow_up_suggestion"
            assert payload["grounding"]["source"] == "sql"

        deliveries = (
            await session.execute(
                text(
                    "SELECT priority, event_type, status FROM notification_deliveries"
                    " WHERE tenant_id = :tid ORDER BY event_type"
                ),
                {"tid": workspace.tenant_a},
            )
        ).all()
        assert len(deliveries) == 5
        assert {row[2] for row in deliveries} == {"pending"}
        priorities = {str(row[1]): str(row[0]) for row in deliveries}
        assert priorities["automation.negative_margin_hold"] == "P0"
        assert priorities["automation.daily_patrol_brief"] == "P2"
        assert priorities["automation.low_doc_replenishment"] == "P1"

        # Re-running never duplicates a pending L1 proposal for the same entity.
        second = await automation.evaluate_rules(
            session,
            workspace.tenant_a,
            notification_service=service,
        )
        assert second.seeded_defaults is False
        assert second.total_proposals_created == 0
        assert second.total_matched == first.total_matched
        assert second.total_notifications_queued == 5
        count = await session.scalar(
            text("SELECT COUNT(*) FROM ai_proposals WHERE tenant_id = :tid"),
            {"tid": workspace.tenant_a},
        )
        assert int(count or 0) == 3


async def test_automation_disabled_rule_is_skipped_and_thresholds_are_editable(
    workspace: Workspace,
) -> None:
    stub = StubTransport()
    service = service_with(stub)
    automation = AutomationService()
    async with tenant_session(workspace.factory, workspace.tenant_a) as session:
        seeded = await automation.seed_default_rules(session, workspace.tenant_a)
        assert seeded.created == 5
        again = await automation.seed_default_rules(session, workspace.tenant_a)
        assert again.created == 0
        assert again.existing == 5

        low_doc = next(
            rule for rule in seeded.rules if rule.template_code == "low_doc_replenishment"
        )
        updated = await automation.update_rule(
            session,
            workspace.tenant_a,
            low_doc.id,
            is_enabled=False,
            priority="P2",
            config_json={"doc_days": 45},
        )
        assert updated.is_enabled is False
        assert updated.priority == "P2"
        assert updated.config_json["doc_days"] == 45
        assert updated.config_json["safety_stock_qty"] == 50

        report = await automation.evaluate_rules(
            session, workspace.tenant_a, dry_run=True, notification_service=service
        )
        by_code = {item.template_code: item for item in report.rules}
        assert by_code["low_doc_replenishment"].matched_count == 0
        assert by_code["low_doc_replenishment"].is_enabled is False

        # An invalid threshold is rejected before it reaches JSONB.
        with pytest.raises(AppError) as exc_info:
            await automation.update_rule(
                session,
                workspace.tenant_a,
                low_doc.id,
                config_json={"doc_days": "not-a-number"},
            )
        assert exc_info.value.code == "ERR_AUTOMATION_CONFIG_INVALID"


async def test_router_automation_endpoints_drive_a_full_cycle(workspace: Workspace) -> None:
    stub = StubTransport()
    service = service_with(stub)
    client = client_for(
        workspace.factory, workspace.principal(workspace.owner_a, UserRole.OWNER), service
    )
    async with client:
        empty = await client.get(f"{BASE}/automation-rules")
        assert empty.status_code == 200
        assert empty.json() == []

        seeded = await client.post(f"{BASE}/automation-rules/seed-defaults")
        assert seeded.status_code == 201, seeded.text
        assert seeded.json()["created"] == 5
        rule_id = seeded.json()["rules"][0]["id"]

        channel = await client.post(
            f"{BASE}/notifications/channels",
            json={
                "name": "Ops WeCom",
                "provider": "wecom",
                "webhook_url": "http://127.0.0.1:9996/hook",
                "min_priority": "P2",
            },
        )
        assert channel.status_code == 201, channel.text

        patched = await client.patch(
            f"{BASE}/automation-rules/{rule_id}",
            json={"priority": "P2", "is_enabled": True, "config_json": {"doc_days": 20}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["priority"] == "P2"

        dry = await client.post(f"{BASE}/automation-rules/evaluate", json={"dry_run": True})
        assert dry.status_code == 200, dry.text
        dry_body = dry.json()
        assert dry_body["dry_run"] is True
        assert dry_body["total_proposals_created"] == 0
        assert dry_body["total_matched"] >= 3

        executed = await client.post(f"{BASE}/automation-rules/evaluate", json={"dry_run": False})
        assert executed.status_code == 200, executed.text
        executed_body = executed.json()
        assert executed_body["dry_run"] is False
        assert executed_body["total_proposals_created"] == 3
        assert executed_body["total_notifications_queued"] == 5

        log = await client.get(f"{BASE}/notifications/deliveries")
        assert log.status_code == 200
        assert log.json()["total"] == 5
        assert all(row["status"] == "pending" for row in log.json()["items"])
