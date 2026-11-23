from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.webhook_store import accept_raw_webhook_event
from erp_core.shared.config import get_settings
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.redis_client import create_redis_client


async def test_17track_fetch_requests_cache_level_zero_and_rejects_paid_refresh() -> None:
    from erp_core.orders.tracking_adapter import Tracking17TrackAdapter

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"accepted": [], "rejected": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adapter = Tracking17TrackAdapter(api_key="test-key", http_client=http)
        assert (await adapter.register_tracking("YT1234", "190002"))["code"] == 0
        assert (await adapter.fetch_status("YT1234"))["code"] == 0
        for invalid in (1, -1, False):
            with pytest.raises(ValueError, match="cacheLevel"):
                await adapter.fetch_status("YT1234", cache_level=invalid)

    assert [r.url.path for r in calls] == ["/track/v2.2/register", "/track/v2.2/gettrackinfo"]
    assert all(r.headers["17token"] == "test-key" for r in calls)
    assert json.loads(calls[0].content) == [{"number": "YT1234", "carrier": 190002}]
    assert json.loads(calls[1].content) == [{"number": "YT1234", "cacheLevel": 0}]


def test_17track_verifies_original_raw_bytes() -> None:
    from erp_core.orders.tracking_adapter import verify_17track_signature

    raw = '{ "description" : "已签收" }\n'.encode()
    secret = "webhook-secret"
    sign = hashlib.sha256(raw + b"/" + secret.encode()).hexdigest()
    assert verify_17track_signature(raw, sign, secret)
    assert not verify_17track_signature(raw.strip(), sign, secret)
    assert not verify_17track_signature(raw, "invalid", secret)
    assert not verify_17track_signature(raw, "非ASCII", secret)
    assert not verify_17track_signature(raw, sign, "")


@pytest.mark.parametrize(
    ("main", "sub", "expected"),
    [
        (20, "InTransit_Departure", ("in_transit", "departure")),
        ("DeliveryFailure", "DeliveryFailure_InvalidAddress", ("undelivered", "invalid_address")),
        ("Exception", "Exception_CustomsHold", ("alert", "customs_hold")),
        ("Delivered", None, ("delivered", None)),
    ],
)
def test_17track_maps_provider_statuses(
    main: str | int, sub: str | None, expected: tuple[str, str | None]
) -> None:
    from erp_core.orders.tracking_adapter import map_17track_status

    assert map_17track_status(main, sub) == expected


async def test_17track_cron_is_registered_on_the_hour() -> None:
    from taskiq.schedule_sources import LabelScheduleSource

    from erp_core.worker_main import scheduler

    schedules = []
    for source in scheduler.sources:
        if isinstance(source, LabelScheduleSource):
            await source.startup()
            try:
                schedules.extend(await source.get_schedules())
            finally:
                await source.shutdown()

    tracking_crons = [
        task.cron
        for task in schedules
        if task.task_name.endswith(":monitor_stalled_shipments_cron")
    ]
    assert tracking_crons == ["0 * * * *"]


async def test_17track_cron_emits_each_stalled_or_alert_shipment_once_under_rls(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from erp_core.orders.tracking_worker import monitor_stalled_shipments_cron

    tenant_ids = [uuid4(), uuid4()]
    tenant_strings = {str(tid) for tid in tenant_ids}
    ship_ids = [uuid4() for _ in range(3)]
    now = datetime.now(UTC)
    redis = create_redis_client(get_settings())
    stream = "stream:erp:events"
    prior_entries = await redis.xrevrange(stream, count=1)
    cursor = f"({prior_entries[0][0]}" if prior_entries else "-"

    try:
        for tenant_index, tenant_id in enumerate(tenant_ids):
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                customer_id, order_id = uuid4(), uuid4()
                await session.execute(
                    text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Tracking', :slug)"),
                    {"tid": tenant_id, "slug": f"tracking-{tenant_id.hex}"},
                )
                await session.execute(
                    text(
                        "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                        "VALUES (:cid, :tid, 'b2c_consumer', 'Tracking customer')"
                    ),
                    {"cid": customer_id, "tid": tenant_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, "
                        "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                        "fulfillment_status, payment_status) VALUES (:oid, :tid, 'ORDER-TRACK', "
                        "'b2c_retail', :cid, 'USD', 7, 50, 50, 'shipped', 'paid')"
                    ),
                    {"oid": order_id, "cid": customer_id, "tid": tenant_id},
                )
                cases = (
                    [(ship_ids[0], "in_transit", 73), (ship_ids[2], "in_transit", 71)]
                    if tenant_index == 0
                    else [(ship_ids[1], "alert", 1)]
                )
                for sid, status, hours in cases:
                    await session.execute(
                        text(
                            "INSERT INTO shipments (id, tenant_id, order_id, tracking_provider, "
                            "tracking_number, standardized_status, last_event_at) "
                            "VALUES (:sid, :tid, :oid, '17track', :number, :status, :event_at)"
                        ),
                        {
                            "sid": sid,
                            "tid": tenant_id,
                            "oid": order_id,
                            "number": f"track-{sid.hex}",
                            "status": status,
                            "event_at": now - timedelta(hours=hours),
                        },
                    )

        async with system_session(runtime_session_factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM shipments")) == 0

        assert await monitor_stalled_shipments_cron() >= 2
        await monitor_stalled_shipments_cron()
        all_entries = await redis.xrange(stream, min=cursor)
        entries = [event for _, event in all_entries if event.get("tenant_id") in tenant_strings]
        assert len(entries) == 2
        assert {e["shipment_id"]: (e["tenant_id"], e["trigger_reason"]) for e in entries} == {
            str(ship_ids[0]): (str(tenant_ids[0]), "in_transit_stalled_72h"),
            str(ship_ids[1]): (str(tenant_ids[1]), "status_alert"),
        }
        assert all(e["event_type"] == "PROACTIVE_WISMO_OUTREACH" for e in entries)

        for tenant_index, tenant_id in enumerate(tenant_ids):
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                flags = dict(
                    (
                        await session.execute(
                            text("SELECT id, exception_alert_sent FROM shipments")
                        )
                    ).all()
                )
                expected_flags = (
                    {ship_ids[0]: True, ship_ids[2]: False}
                    if tenant_index == 0
                    else {ship_ids[1]: True}
                )
                assert flags == expected_flags
    finally:
        for tenant_id in tenant_ids:
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                await session.execute(text("DELETE FROM shipments"))
                await session.execute(text("DELETE FROM orders"))
                await session.execute(text("DELETE FROM customers"))
                await session.execute(
                    text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id}
                )
        for entry_id, event in await redis.xrange(stream, min=cursor):
            if event.get("tenant_id") in tenant_strings:
                await redis.xdel(stream, entry_id)
        for ship_id in ship_ids:
            await redis.delete(f"erp:wismo:published:{ship_id}")
        await redis.aclose()


async def test_17track_uses_the_existing_database_ingress_allowlist(
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = uuid4()
    async with system_session(migrator_session_factory) as session:
        assert (
            await accept_raw_webhook_event(
                session,
                event_id=event_id,
                tenant_id=None,
                channel_type="17track",
                external_event_id=str(event_id),
                external_account_id=None,
                headers_json="{}",
                payload_json="{}",
            )
            == event_id
        )
        await session.execute(
            text("DELETE FROM raw_webhook_events WHERE id = :id"), {"id": event_id}
        )


async def test_signed_17track_webhook_is_persisted_once_and_delivers_shipment(
    async_client: httpx.AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, customer_id, order_id, shipment_id = (uuid4() for _ in range(4))
    number = f"track-{shipment_id.hex}"
    secret = "raw-bytes-secret"
    monkeypatch.setenv("TRACK17_WEBHOOK_SECRET", secret)
    event_at = datetime.now(UTC)
    event = {"time_utc": event_at.isoformat(), "description": "Signed"}
    raw = json.dumps(
        {
            "event": "TRACKING_UPDATED",
            "data": {
                "number": number,
                "track_info": {"latest_status": {"status": "Delivered"}, "latest_event": event},
            },
        }
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    header = {"sign": hashlib.sha256(raw + b"/" + secret.encode()).hexdigest()}
    path = "/api/v1/webhooks/17track"
    redis = create_redis_client(get_settings())
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Track', :slug)"),
                {"tid": tenant_id, "slug": f"tracking-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:cid, :tid, 'b2c_consumer', 'Tracking customer')"
                ),
                {"cid": customer_id, "tid": tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, "
                    "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                    "fulfillment_status, payment_status) VALUES (:oid, :tid, 'ORDER-TRACK', "
                    "'b2c_retail', :cid, 'USD', 7, 50, 50, 'shipped', 'paid')"
                ),
                {"oid": order_id, "cid": customer_id, "tid": tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO shipments (id, tenant_id, order_id, tracking_provider, "
                    "tracking_number, standardized_status) "
                    "VALUES (:sid, :tid, :oid, '17track', :number, 'in_transit')"
                ),
                {"sid": shipment_id, "tid": tenant_id, "oid": order_id, "number": number},
            )

        bad = await async_client.post(path, content=raw + b" ", headers=header)
        assert bad.status_code == 401
        assert (await async_client.post(path, content=raw, headers=header)).status_code == 200
        assert (await async_client.post(path, content=raw, headers=header)).status_code == 200
        async with system_session(migrator_session_factory) as session:
            receipt_id: UUID = (
                await session.execute(
                    text(
                        "SELECT id FROM raw_webhook_events WHERE channel_type = '17track' "
                        "AND raw_payload->'data'->>'number' = :n"
                    ),
                    {"n": number},
                )
            ).scalar_one()

        from erp_core.channels.webhook_delivery import drain_deliveries
        from erp_core.orders.tracking_worker import process_17track_webhook

        queued = await redis.xrange("stream:webhooks:17track")
        assert sum(fields["raw_event_id"] == str(receipt_id) for _, fields in queued) == 1
        await drain_deliveries(migrator_session_factory, redis, "17track", process_17track_webhook)
        assert await process_17track_webhook(str(receipt_id)) == 0
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            tracking = (
                await session.execute(
                    text(
                        "SELECT standardized_status, last_event_at, tracking_events FROM shipments"
                    )
                )
            ).one()
            assert tuple(tracking) == ("delivered", event_at, [event])
            assert (
                await session.scalar(text("SELECT fulfillment_status FROM orders")) == "delivered"
            )
            receipt = (
                await session.execute(
                    text(
                        "SELECT processing_status, external_event_id FROM raw_webhook_events "
                        "WHERE id = :id"
                    ),
                    {"id": receipt_id},
                )
            ).one()
            assert tuple(receipt) == ("processed", digest)
    finally:
        await redis.aclose()
        async with system_session(migrator_session_factory) as session:
            await session.execute(
                text(
                    "DELETE FROM raw_webhook_events WHERE channel_type = '17track' "
                    "AND raw_payload->'data'->>'number' = :n"
                ),
                {"n": number},
            )
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(text("DELETE FROM shipments"))
            await session.execute(text("DELETE FROM orders"))
            await session.execute(text("DELETE FROM customers"))
            await session.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})


async def test_ship_order_unconfigured_17track_raises_422_instead_of_500(
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from erp_core.orders.order_service import OrderService
    from erp_core.orders.schemas import ShipOrderRequest
    from erp_core.orders.tracking_adapter import Tracking17TrackAdapter
    from erp_core.shared.enums import UserRole
    from erp_core.shared.errors import AppError
    from erp_core.tenancy.dependencies import CurrentPrincipal

    tenant_id, customer_id, order_id, warehouse_id, variant_id = (uuid4() for _ in range(5))
    product_id = uuid4()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Ship422', :slug)"),
            {"tid": tenant_id, "slug": f"ship422-{tenant_id.hex}"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2c_consumer', 'Ship Buyer')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code) "
                "VALUES (:wid, :tid, 'WH-1', 'Main', 'CN')"
            ),
            {"wid": warehouse_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, 'SPU-S422', 'Lighting')"
            ),
            {"pid": product_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base, "
                "retail_price_usd, weight_grams) VALUES (:vid, :tid, :pid, 'SKU-S422', 5, 10, 100)"
            ),
            {"vid": variant_id, "tid": tenant_id, "pid": product_id},
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 10, 1)"
            ),
            {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
        )
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, "
                "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                "fulfillment_status, payment_status) VALUES (:oid, :tid, 'ORD-S422', "
                "'b2c_retail', :cid, 'USD', 1, 10, 10, 'confirmed_reserved', 'paid')"
            ),
            {"oid": order_id, "tid": tenant_id, "cid": customer_id},
        )
        await session.execute(
            text(
                "INSERT INTO order_items (id, tenant_id, order_id, variant_id, sku_snapshot, "
                "quantity, unit_price, unit_cost_base_snapshot, line_total) "
                "VALUES (:iid, :tid, :oid, :vid, 'SKU-S422', 1, 10, 5, 10)"
            ),
            {"iid": uuid4(), "tid": tenant_id, "oid": order_id, "vid": variant_id},
        )

    principal = CurrentPrincipal(
        user_id=uuid4(),
        tenant_id=tenant_id,
        role=UserRole.OWNER,
        email="owner@ship422.example.com",
    )
    try:
        with pytest.raises(AppError) as exc_info:
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                await OrderService().ship_order(
                    session,
                    tenant_id,
                    principal=principal,
                    order_id=order_id,
                    payload=ShipOrderRequest(warehouse_id=warehouse_id, tracking_number="TRK123"),
                    tracking_adapter=Tracking17TrackAdapter(api_key=""),
                )
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "ERR_TRACKING_PROVIDER_NOT_CONFIGURED"
    finally:
        async with system_session(migrator_session_factory) as session:
            for tbl in (
                "inventory_ledger",
                "inventory_balances",
                "shipments",
                "order_items",
                "orders",
                "product_variants",
                "products",
                "warehouses",
                "customers",
            ):
                await session.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await session.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
