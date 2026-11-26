"""Inventory chaos and seeded property-based concurrency suite (Track 6).

Two independent guarantees are exercised against the real PG17 test database as
the non-superuser `app_runtime` role inside `tenant_session` (FORCE RLS):

1. `test_seeded_property_state_machines_preserve_ledger_invariants`
   A deterministic seeded state machine (64 schedules x 8 steps) replays a
   randomized but reproducible mix of `inbound_stock`, `reserve_for_order`,
   `release_reservation`, `deduct_shipped`, and `adjust_stocktake` across
   several warehouses and variants - including deliberately duplicated
   idempotency keys. After EVERY step the live balance row must equal an
   independent in-memory reference model, and the append-only ledger must still
   sum to it:

       on_hand_qty >= 0, reserved_qty >= 0, on_hand_qty >= reserved_qty
       SUM(qty_delta_on_hand) == on_hand_qty
       SUM(qty_delta_reserved) == reserved_qty

2. `test_chaos_concurrency_storm_no_oversell_no_deadlock`
   25 parallel `asyncio.gather` reservation tasks (three variants per task,
   shuffled input order, duplicated idempotency prefixes, finite shared stock)
   must produce zero deadlocks, zero negative availability, exact unit
   conservation between successful reservations and `reserved_qty`, and a
   single coalesced `inventory_sync_jobs` outbox row per platform mapping.

`inventory_ledger` is append-only (DELETE is revoked from `app_runtime`), so the
products/variants/tenants these rows point at are intentionally retained after
the suite, exactly like `test_inventory_concurrency.py`.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.orders.inventory_service import InsufficientStockError, InventoryLedgerService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError

SCHEDULE_COUNT = 64
STEPS_PER_SCHEDULE = 8
WAREHOUSE_COUNT = 3
VARIANT_COUNT = 4
STORM_TASKS = 25
STORM_DISTINCT_PREFIXES = 13
STORM_STOCK_PER_VARIANT = 10

Balance = dict[str, int]


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


# ----------------------------------------------------------------------------
# Seeding
# ----------------------------------------------------------------------------


async def _create_tenant(factory: async_sessionmaker[AsyncSession], label: str) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, :name, :slug)"),
            {
                "tid": tenant_id,
                "name": f"{label} Co",
                "slug": f"{label.lower()}-{tenant_id.hex[:10]}",
            },
        )
    return tenant_id


async def _create_variant(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    sku: str,
    *,
    index: int,
) -> UUID:
    product_id, variant_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, :spu, 'lighting')"
            ),
            {"pid": product_id, "tid": tenant_id, "spu": f"SPU-CHAOS-{index}"},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, "
                "cost_price_base, retail_price_usd, weight_grams) "
                "VALUES (:vid, :tid, :pid, :sku, 12.0000, 19.99, 300)"
            ),
            {"vid": variant_id, "tid": tenant_id, "pid": product_id, "sku": sku},
        )
    return variant_id


async def _create_mapped_channel_account(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    variant_id: UUID,
    *,
    external_sku: str,
) -> tuple[UUID, UUID]:
    account_id, mapping_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status) "
                "VALUES (:aid, :tid, 'shopify', :external, 'Chaos store', 'active')"
            ),
            {"aid": account_id, "tid": tenant_id, "external": f"store-{account_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO platform_sku_mappings (id, tenant_id, variant_id, "
                "channel_account_id, external_product_id, external_sku, "
                "sync_inventory_enabled, safety_stock, bundle_multiplier) "
                "VALUES (:mid, :tid, :vid, :aid, :ext_product, :ext_sku, true, 0, 1)"
            ),
            {
                "mid": mapping_id,
                "tid": tenant_id,
                "vid": variant_id,
                "aid": account_id,
                "ext_product": f"ext-{mapping_id.hex[:8]}",
                "ext_sku": external_sku,
            },
        )
    return account_id, mapping_id


# ----------------------------------------------------------------------------
# Live-state snapshot + invariant assertions
# ----------------------------------------------------------------------------


async def snapshot(
    session: AsyncSession,
    tenant_id: UUID,
    warehouse_ids: list[UUID],
    variant_ids: list[UUID],
) -> dict[tuple[UUID, UUID], Balance]:
    balance_stmt = text(
        "SELECT warehouse_id, variant_id, on_hand_qty, reserved_qty "
        "FROM inventory_balances "
        "WHERE tenant_id = :tid AND warehouse_id IN :wids AND variant_id IN :vids"
    ).bindparams(
        bindparam("wids", expanding=True),
        bindparam("vids", expanding=True),
    )
    ledger_stmt = text(
        "SELECT warehouse_id, variant_id, "
        "COALESCE(SUM(qty_delta_on_hand), 0) AS sum_on_hand, "
        "COALESCE(SUM(qty_delta_reserved), 0) AS sum_reserved "
        "FROM inventory_ledger "
        "WHERE tenant_id = :tid AND warehouse_id IN :wids AND variant_id IN :vids "
        "GROUP BY warehouse_id, variant_id"
    ).bindparams(
        bindparam("wids", expanding=True),
        bindparam("vids", expanding=True),
    )
    params: dict[str, Any] = {"tid": tenant_id, "wids": warehouse_ids, "vids": variant_ids}

    state: dict[tuple[UUID, UUID], Balance] = {
        (warehouse_id, variant_id): {
            "on_hand": 0,
            "reserved": 0,
            "sum_on_hand": 0,
            "sum_reserved": 0,
        }
        for warehouse_id in warehouse_ids
        for variant_id in variant_ids
    }
    for row in (await session.execute(balance_stmt, params)).all():
        key = (row.warehouse_id, row.variant_id)
        state[key]["on_hand"] = int(row.on_hand_qty)
        state[key]["reserved"] = int(row.reserved_qty)
    for row in (await session.execute(ledger_stmt, params)).all():
        key = (row.warehouse_id, row.variant_id)
        state[key]["sum_on_hand"] = int(row.sum_on_hand)
        state[key]["sum_reserved"] = int(row.sum_reserved)
    return state


def assert_invariants(
    state: dict[tuple[UUID, UUID], Balance],
    model: dict[tuple[UUID, UUID], Balance],
    *,
    label: str,
) -> None:
    for key, expected in model.items():
        actual = state[key]
        assert actual["on_hand"] >= 0, f"{label} {key}: negative on_hand"
        assert actual["reserved"] >= 0, f"{label} {key}: negative reserved"
        assert actual["on_hand"] >= actual["reserved"], f"{label} {key}: on_hand < reserved"
        assert actual["on_hand"] == expected["on_hand"], (
            f"{label} {key}: on_hand {actual['on_hand']} != model {expected['on_hand']}"
        )
        assert actual["reserved"] == expected["reserved"], (
            f"{label} {key}: reserved {actual['reserved']} != model {expected['reserved']}"
        )
        assert actual["sum_on_hand"] == actual["on_hand"], (
            f"{label} {key}: ledger on_hand {actual['sum_on_hand']} != balance {actual['on_hand']}"
        )
        assert actual["sum_reserved"] == actual["reserved"], (
            f"{label} {key}: ledger reserved {actual['sum_reserved']} != balance "
            f"{actual['reserved']}"
        )


# ----------------------------------------------------------------------------
# Reference model state machine
# ----------------------------------------------------------------------------


@dataclass
class Reference:
    """Independent in-memory replay of the ledger's documented semantics."""

    balances: dict[tuple[UUID, UUID], Balance] = field(default_factory=dict)
    inbound_keys: set[str] = field(default_factory=set)
    stocktake_keys: set[str] = field(default_factory=set)
    order_keys: set[tuple[str, str, UUID]] = field(default_factory=set)
    history: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))

    def balance(self, warehouse_id: UUID, variant_id: UUID) -> Balance:
        return self.balances.setdefault((warehouse_id, variant_id), {"on_hand": 0, "reserved": 0})

    def available(self, warehouse_id: UUID, variant_id: UUID) -> int:
        row = self.balances.get((warehouse_id, variant_id))
        if row is None:
            return 0
        return row["on_hand"] - row["reserved"]


def _sorted_items(items: list[tuple[UUID, int]]) -> list[tuple[UUID, int]]:
    merged: dict[UUID, int] = defaultdict(int)
    for variant_id, qty in items:
        merged[variant_id] += qty
    return sorted(merged.items(), key=lambda pair: str(pair[0]))


async def _run_step(
    service: InventoryLedgerService,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    rng: random.Random,
    reference: Reference,
    warehouse_ids: list[UUID],
    variant_ids: list[UUID],
    label: str,
) -> str:
    """Execute one randomized operation, then mutate the model only on success."""
    kind = rng.choices(
        ["inbound", "reserve", "release", "deduct", "stocktake"],
        weights=[28, 30, 16, 10, 16],
    )[0]
    warehouse_id = rng.choice(warehouse_ids)
    touched = rng.sample(variant_ids, k=min(rng.choice([1, 1, 2, 3]), len(variant_ids)))
    items = [(variant_id, rng.randint(1, 3)) for variant_id in touched]
    rng.shuffle(items)

    if kind == "inbound":
        variant_id = touched[0]
        qty = rng.randint(1, 6)
        cached = reference.history["inbound_keys"]
        key = (
            rng.choice(cached)
            if cached and rng.random() < 0.18
            else f"{label}-in-{rng.randrange(10**6)}"
        )
        try:
            async with tenant_session(factory, tenant_id) as session:
                await service.inbound_stock(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    variant_id=variant_id,
                    qty=qty,
                    reference_id=uuid4(),
                    idempotency_key=key,
                )
        except AppError:  # pragma: no cover - inbound never fails on valid input
            raise
        if key not in reference.inbound_keys:
            row = reference.balance(warehouse_id, variant_id)
            row["on_hand"] += qty
            reference.inbound_keys.add(key)
            reference.history["inbound_keys"].append(key)
            return "inbound"
        return "inbound-duplicate"

    if kind == "stocktake":
        variant_id = touched[0]
        delta = rng.choice([-3, -2, -1, 1, 2, 4])
        cached = reference.history["stocktake_keys"]
        key = (
            rng.choice(cached)
            if cached and rng.random() < 0.18
            else f"{label}-st-{rng.randrange(10**6)}"
        )
        if key in reference.stocktake_keys:
            return "stocktake-duplicate"
        row = reference.balances.get((warehouse_id, variant_id))
        if delta > 0:
            will_succeed = True  # a positive count creates the balance row if missing
        else:
            will_succeed = row is not None and (row["on_hand"] - row["reserved"]) >= -delta
        try:
            async with tenant_session(factory, tenant_id) as session:
                await service.adjust_stocktake(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    variant_id=variant_id,
                    qty_delta=delta,
                    reference_id=uuid4(),
                    idempotency_key=key,
                )
        except AppError:
            assert not will_succeed, (
                f"{label}: stocktake {delta} unexpectedly rejected for {variant_id}"
            )
            return "stocktake-rejected"
        assert will_succeed, f"{label}: stocktake {delta} unexpectedly accepted"
        row = reference.balance(warehouse_id, variant_id)
        row["on_hand"] += delta
        reference.stocktake_keys.add(key)
        reference.history["stocktake_keys"].append(key)
        return "stocktake"

    order_kind = kind
    cached = reference.history[f"{order_kind}_prefixes"]
    prefix = (
        rng.choice(cached)
        if cached and rng.random() < 0.22
        else f"{label}-{order_kind}-{rng.randrange(10**6)}"
    )
    ordered = _sorted_items(items)
    fresh = [
        (variant_id, qty)
        for variant_id, qty in ordered
        if (order_kind, prefix, variant_id) not in reference.order_keys
    ]
    if not fresh:
        return f"{order_kind}-duplicate"

    if order_kind == "reserve":
        will_succeed = all(
            reference.available(warehouse_id, variant_id) >= qty for variant_id, qty in fresh
        )
    elif order_kind == "release":
        will_succeed = all(
            reference.balances.get((warehouse_id, variant_id), {}).get("reserved", 0) >= qty
            for variant_id, qty in fresh
        )
    else:  # deduct
        will_succeed = all(
            reference.balances.get((warehouse_id, variant_id), {}).get("on_hand", 0) >= qty
            and reference.balances.get((warehouse_id, variant_id), {}).get("reserved", 0) >= qty
            for variant_id, qty in fresh
        )

    try:
        async with tenant_session(factory, tenant_id) as session:
            if order_kind == "reserve":
                await service.reserve_for_order(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    order_id=uuid4(),
                    items=list(items),
                    idempotency_prefix=prefix,
                )
            elif order_kind == "release":
                await service.release_reservation(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    order_id=uuid4(),
                    items=list(items),
                    idempotency_prefix=prefix,
                )
            else:
                await service.deduct_shipped(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    shipment_id=uuid4(),
                    items=list(items),
                    idempotency_prefix=prefix,
                )
    except InsufficientStockError:
        assert not will_succeed, f"{label}: {order_kind} unexpectedly rejected"
        return f"{order_kind}-rejected"
    except AppError:
        assert not will_succeed, f"{label}: {order_kind} unexpectedly rejected"
        return f"{order_kind}-rejected"

    assert will_succeed, f"{label}: {order_kind} unexpectedly accepted"
    for variant_id, qty in fresh:
        reference.order_keys.add((order_kind, prefix, variant_id))
        row = reference.balance(warehouse_id, variant_id)
        if order_kind == "reserve":
            row["reserved"] += qty
        elif order_kind == "release":
            row["reserved"] -= qty
        else:
            row["on_hand"] -= qty
            row["reserved"] -= qty
    reference.history[f"{order_kind}_prefixes"].append(prefix)
    return order_kind


async def test_seeded_property_state_machines_preserve_ledger_invariants(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = runtime_session_factory
    service = InventoryLedgerService()
    tenant_id = await _create_tenant(factory, "ChaosProperty")
    warehouse_ids = [uuid4() for _ in range(WAREHOUSE_COUNT)]
    variant_ids = [
        await _create_variant(factory, tenant_id, f"CHAOS-{index:02d}", index=index)
        for index in range(VARIANT_COUNT)
    ]

    outcomes: dict[str, int] = defaultdict(int)

    for schedule in range(SCHEDULE_COUNT):
        rng = random.Random(10_000 + schedule)
        schedule_warehouses = rng.sample(
            warehouse_ids, k=rng.choice([1, 2, min(3, WAREHOUSE_COUNT)])
        )
        schedule_variants = rng.sample(variant_ids, k=rng.choice([1, 2, min(3, VARIANT_COUNT)]))
        # The reference model is bootstrapped from the live snapshot taken at t0 of
        # this schedule; every subsequent step is then asserted exactly against it,
        # so any drift introduced by the ledger engine is caught at that step.
        reference = Reference()
        async with tenant_session(factory, tenant_id) as session:
            bootstrap = await snapshot(session, tenant_id, schedule_warehouses, schedule_variants)
        reference.balances = {
            key: {"on_hand": row["on_hand"], "reserved": row["reserved"]}
            for key, row in bootstrap.items()
        }

        for step in range(STEPS_PER_SCHEDULE):
            label = f"s{schedule:02d}-{step:02d}"
            outcome = await _run_step(
                service,
                factory,
                tenant_id,
                rng,
                reference,
                schedule_warehouses,
                schedule_variants,
                label,
            )
            outcomes[outcome] += 1

            async with tenant_session(factory, tenant_id) as session:
                state = await snapshot(session, tenant_id, schedule_warehouses, schedule_variants)
            assert_invariants(state, reference.balances, label=label)

        # Final sweep of the whole schedule grid after the last step.
        async with tenant_session(factory, tenant_id) as session:
            final_state = await snapshot(session, tenant_id, schedule_warehouses, schedule_variants)
        assert_invariants(final_state, reference.balances, label=f"s{schedule:02d}-final")

    # The generator really exercised every branch, including duplicates and rejections.
    assert sum(outcomes.values()) == SCHEDULE_COUNT * STEPS_PER_SCHEDULE
    for expected_kind in ("inbound", "reserve", "release", "deduct", "stocktake"):
        assert outcomes[expected_kind] > 0, f"{expected_kind} never succeeded"
    assert any(name.endswith("-duplicate") and outcomes[name] > 0 for name in outcomes), (
        "duplicate idempotency keys were never replayed"
    )
    assert outcomes["reserve-rejected"] > 0, "oversell contention was never exercised"
    assert outcomes["deduct-rejected"] > 0, "invalid shipment deduction was never exercised"
    assert outcomes["stocktake-rejected"] > 0, "stocktake rollback was never exercised"

    # Whole-tenant conservation: the ledger still explains every balance row.
    async with tenant_session(factory, tenant_id) as session:
        drift = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*) AS drift_rows
                    FROM inventory_balances b
                    WHERE b.tenant_id = :tid
                      AND (
                        b.on_hand_qty <> COALESCE((
                            SELECT SUM(l.qty_delta_on_hand) FROM inventory_ledger l
                            WHERE l.tenant_id = b.tenant_id
                              AND l.warehouse_id = b.warehouse_id
                              AND l.variant_id = b.variant_id
                        ), 0)
                        OR b.reserved_qty <> COALESCE((
                            SELECT SUM(l.qty_delta_reserved) FROM inventory_ledger l
                            WHERE l.tenant_id = b.tenant_id
                              AND l.warehouse_id = b.warehouse_id
                              AND l.variant_id = b.variant_id
                        ), 0)
                      )
                    """
                ),
                {"tid": tenant_id},
            )
        ).scalar_one()
    assert int(drift) == 0


# ----------------------------------------------------------------------------
# Chaos concurrency storm
# ----------------------------------------------------------------------------


def _is_deadlock(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "deadlock" in message or "serialization" in message


async def test_chaos_concurrency_storm_no_oversell_no_deadlock() -> None:
    engine = create_async_engine(_runtime_url(), echo=False, pool_size=30, max_overflow=10)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = InventoryLedgerService()

    tenant_id = uuid4()
    warehouse_id = uuid4()
    variant_ids = [uuid4() for _ in range(3)]

    try:
        async with tenant_session(factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, :name, :slug)"),
                {
                    "tid": tenant_id,
                    "name": "Chaos Storm Co",
                    "slug": f"chaos-storm-{tenant_id.hex[:10]}",
                },
            )
            product_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO products (id, tenant_id, spu_code, category) "
                    "VALUES (:pid, :tid, 'SPU-STORM', 'lighting')"
                ),
                {"pid": product_id, "tid": tenant_id},
            )
            for index, variant_id in enumerate(variant_ids):
                await session.execute(
                    text(
                        "INSERT INTO product_variants (id, tenant_id, product_id, sku, "
                        "cost_price_base, retail_price_usd, weight_grams) "
                        "VALUES (:vid, :tid, :pid, :sku, 12.0000, 19.99, 300)"
                    ),
                    {
                        "vid": variant_id,
                        "tid": tenant_id,
                        "pid": product_id,
                        "sku": f"STORM-{index:02d}",
                    },
                )

        account_id, mapping_id = await _create_mapped_channel_account(
            factory, tenant_id, variant_ids[0], external_sku="STORM-00"
        )

        for variant_id in variant_ids:
            async with tenant_session(factory, tenant_id) as session:
                await service.inbound_stock(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    variant_id=variant_id,
                    qty=STORM_STOCK_PER_VARIANT,
                    reference_id=uuid4(),
                    idempotency_key=f"storm-inbound-{variant_id}",
                )

        async def _attempt(task_index: int) -> dict[str, Any]:
            rng = random.Random(task_index)
            prefix = f"storm-p-{task_index % STORM_DISTINCT_PREFIXES}"
            items = [(variant_id, 1) for variant_id in variant_ids]
            rng.shuffle(items)
            try:
                async with tenant_session(factory, tenant_id) as session:
                    ledger_ids = await service.reserve_for_order(
                        session,
                        tenant_id=tenant_id,
                        warehouse_id=warehouse_id,
                        order_id=uuid4(),
                        items=items,
                        idempotency_prefix=prefix,
                    )
            except InsufficientStockError:
                return {"task": task_index, "status": "insufficient", "units": 0}
            except AppError as exc:  # pragma: no cover - defensive
                return {
                    "task": task_index,
                    "status": "deadlock" if _is_deadlock(exc) else "app_error",
                    "detail": str(exc),
                    "units": 0,
                }
            except Exception as exc:  # pragma: no cover - defensive
                return {
                    "task": task_index,
                    "status": "deadlock" if _is_deadlock(exc) else "error",
                    "detail": str(exc),
                    "units": 0,
                }
            if not ledger_ids:
                return {"task": task_index, "status": "duplicate", "units": 0}
            return {"task": task_index, "status": "reserved", "units": len(ledger_ids)}

        results = await asyncio.gather(*[_attempt(i) for i in range(STORM_TASKS)])
        statuses = defaultdict(int)
        for result in results:
            statuses[result["status"]] += 1
        failures = [result for result in results if result["status"] in {"error", "app_error"}]

        assert not [result for result in results if result["status"] == "deadlock"], (
            f"deadlock/serialization failures: {results}"
        )
        assert failures == [], f"unexpected exceptions: {failures}"

        reserved_units = sum(result["units"] for result in results)
        # Exactly the finite stock is reserved: no oversell, no lost unit.
        assert statuses["reserved"] == STORM_STOCK_PER_VARIANT
        assert reserved_units == STORM_STOCK_PER_VARIANT * len(variant_ids)
        assert statuses["duplicate"] > 0, "duplicated idempotency prefixes never collided"
        assert statuses["insufficient"] > 0, "oversell contention was never exercised"
        assert statuses["duplicate"] + statuses["insufficient"] + statuses["reserved"] == (
            STORM_TASKS
        )

        async with tenant_session(factory, tenant_id) as session:
            state = await snapshot(session, tenant_id, [warehouse_id], variant_ids)
            for variant_id in variant_ids:
                row = state[(warehouse_id, variant_id)]
                assert row["on_hand"] == STORM_STOCK_PER_VARIANT
                assert row["reserved"] == STORM_STOCK_PER_VARIANT
                assert row["on_hand"] - row["reserved"] == 0
                assert row["sum_on_hand"] == row["on_hand"]
                assert row["sum_reserved"] == row["reserved"]

            # Exactly one coalesced pending outbox row per platform mapping.
            jobs = (
                await session.execute(
                    text(
                        "SELECT status, attempts, requested_qty, channel_type, external_sku, "
                        "channel_account_id FROM inventory_sync_jobs "
                        "WHERE tenant_id = :tid AND mapping_id = :mid"
                    ),
                    {"tid": tenant_id, "mid": mapping_id},
                )
            ).all()
            assert len(jobs) == 1
            assert jobs[0].status == "pending"
            assert jobs[0].attempts == 0
            assert jobs[0].channel_type == "shopify"
            assert jobs[0].external_sku == "STORM-00"
            assert jobs[0].channel_account_id == account_id
            assert int(jobs[0].requested_qty) == 0  # fully reserved, nothing sellable

            # Releasing the whole reservation makes one unit of headroom real; a
            # clean follow-up movement must refresh the same single pending row.
            for variant_id in variant_ids:
                await service.release_reservation(
                    session,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    order_id=uuid4(),
                    items=[(variant_id, STORM_STOCK_PER_VARIANT - 1)],
                    idempotency_prefix=f"storm-release-{variant_id}",
                )
            await service.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant_ids[0],
                qty=1,
                reference_id=uuid4(),
                idempotency_key="storm-tail-inbound",
            )

        async with tenant_session(factory, tenant_id) as session:
            refreshed = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS job_count, "
                        "COALESCE(MIN(requested_qty), -1) AS min_qty, "
                        "COALESCE(MAX(requested_qty), -1) AS max_qty "
                        "FROM inventory_sync_jobs WHERE tenant_id = :tid AND mapping_id = :mid"
                    ),
                    {"tid": tenant_id, "mid": mapping_id},
                )
            ).one()
            assert int(refreshed.job_count) == 1, "the pending outbox row was not coalesced"
            assert (
                int(refreshed.min_qty)
                == int(refreshed.max_qty)
                == (STORM_STOCK_PER_VARIANT + 1 - 1)
            )

            tail_state = await snapshot(session, tenant_id, [warehouse_id], variant_ids)
            first = tail_state[(warehouse_id, variant_ids[0])]
            assert first["on_hand"] == STORM_STOCK_PER_VARIANT + 1
            assert first["reserved"] == 1
            assert first["sum_on_hand"] == first["on_hand"]
            assert first["sum_reserved"] == first["reserved"]
    finally:
        # `inventory_ledger` is append-only for app_runtime, so the tenant,
        # products and variants stay behind by design; only the mutable,
        # re-creatable rows are cleared.
        async with tenant_session(factory, tenant_id) as session:
            await session.execute(
                text("DELETE FROM inventory_sync_jobs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await session.execute(
                text("DELETE FROM inventory_balances WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await session.execute(
                text("DELETE FROM platform_sku_mappings WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        await engine.dispose()


async def test_compute_sellable_channel_qty_is_exact_under_safety_stock() -> None:
    """Seeded property: channel-sellable units never exceed net-of-safety stock."""
    from erp_core.orders.inventory_service import compute_sellable_channel_qty

    rng = random.Random(424242)
    for _ in range(500):
        available = rng.randint(0, 5_000)
        safety = rng.randint(0, 4_000)
        multiplier = rng.randint(1, 12)
        sellable = compute_sellable_channel_qty(
            available, safety_stock=safety, bundle_multiplier=multiplier
        )
        net = max(0, available - safety)
        assert sellable >= 0
        assert sellable * multiplier <= net
        assert (sellable + 1) * multiplier > net


@pytest.mark.parametrize("seed", [1, 7, 99])
async def test_idempotent_replay_of_the_same_order_prefix_is_a_no_op(
    runtime_session_factory: async_sessionmaker[AsyncSession], seed: int
) -> None:
    """A retried reservation prefix must never double-reserve, at any seed."""
    factory = runtime_session_factory
    service = InventoryLedgerService()
    tenant_id = await _create_tenant(factory, f"Replay{seed}")
    warehouse_id = uuid4()
    variant_id = await _create_variant(factory, tenant_id, f"REPLAY-{seed}", index=seed)
    rng = random.Random(seed)

    async with tenant_session(factory, tenant_id) as session:
        await service.inbound_stock(
            session,
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            qty=5,
            reference_id=uuid4(),
            idempotency_key=f"replay-inbound-{seed}",
        )

    prefix = f"replay-{seed}"
    qty = rng.randint(1, 5)
    for _ in range(4):
        async with tenant_session(factory, tenant_id) as session:
            await service.reserve_for_order(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                order_id=uuid4(),
                items=[(variant_id, qty)],
                idempotency_prefix=prefix,
            )

    async with tenant_session(factory, tenant_id) as session:
        state = await snapshot(session, tenant_id, [warehouse_id], [variant_id])
        row = state[(warehouse_id, variant_id)]
        assert row["on_hand"] == 5
        assert row["reserved"] == qty
        assert row["sum_on_hand"] == 5
        assert row["sum_reserved"] == qty
        ledger_rows = await session.scalar(
            text(
                "SELECT COUNT(*) FROM inventory_ledger "
                "WHERE tenant_id = :tid AND variant_id = :vid AND event_type = 'reserve_order'"
            ),
            {"tid": tenant_id, "vid": variant_id},
        )
        assert int(ledger_rows or 0) == 1
