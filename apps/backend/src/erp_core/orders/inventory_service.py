from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.orders.schemas import (
    InventoryBalanceItemRead,
    InventoryBalanceRead,
    InventoryLedgerRead,
)
from erp_core.shared.enums import InventoryEventType
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse


class InsufficientStockError(AppError):
    """Raised when on_hand_qty - reserved_qty cannot satisfy a reservation."""

    def __init__(self, variant_id: UUID, requested: int, available: int) -> None:
        super().__init__(
            slug="insufficient-stock",
            status_code=409,
            code="ERR_INSUFFICIENT_STOCK",
            title="Insufficient Stock",
            detail=f"Variant '{variant_id}' requested {requested}, available {available}.",
        )
        self.variant_id = variant_id
        self.requested = requested
        self.available = available


def compute_sellable_channel_qty(
    available_qty: int,
    *,
    safety_stock: int = 0,
    bundle_multiplier: int = 1,
) -> int:
    """Compute channel-sellable units/bundles from ERP available stock."""
    net = max(0, int(available_qty) - max(0, int(safety_stock)))
    return net // max(1, int(bundle_multiplier))


class InventoryLedgerService:
    """Deadlock-free, idempotent atomic inventory ledger & balance engine."""

    @staticmethod
    async def _enqueue_sync_jobs_for_variant(
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
        *,
        trigger_source: str,
    ) -> int:
        """Coalesce pending outbox sync jobs for every mapped active channel of ``variant_id``."""
        total_available = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT GREATEST(0, COALESCE(SUM(on_hand_qty - reserved_qty), 0))
                        FROM inventory_balances
                        WHERE tenant_id = :tid AND variant_id = :vid
                        """
                    ),
                    {"tid": tenant_id, "vid": variant_id},
                )
            ).scalar_one()
        )
        mappings = (
            await session.execute(
                text(
                    """
                    SELECT psm.id AS mapping_id,
                           psm.channel_account_id,
                           ca.channel_type,
                           psm.external_sku,
                           psm.safety_stock,
                           psm.bundle_multiplier
                    FROM platform_sku_mappings psm
                    JOIN channel_accounts ca
                      ON ca.id = psm.channel_account_id AND ca.tenant_id = psm.tenant_id
                    WHERE psm.tenant_id = :tid
                      AND psm.variant_id = :vid
                      AND psm.sync_inventory_enabled = true
                      AND ca.status = 'active'
                    """
                ),
                {"tid": tenant_id, "vid": variant_id},
            )
        ).all()
        enqueued = 0
        for row in mappings:
            sellable_qty = compute_sellable_channel_qty(
                total_available,
                safety_stock=int(row.safety_stock or 0),
                bundle_multiplier=int(row.bundle_multiplier or 1),
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_sync_jobs (
                        tenant_id, variant_id, mapping_id, channel_account_id,
                        channel_type, external_sku, requested_qty, status,
                        attempts, next_retry_at, trigger_source
                    ) VALUES (
                        :tid, :vid, :mid, :caid,
                        :ctype, :ext_sku, :qty, 'pending',
                        0, now(), :tsrc
                    )
                    ON CONFLICT (tenant_id, mapping_id) WHERE status = 'pending'
                    DO UPDATE SET
                        requested_qty = EXCLUDED.requested_qty,
                        external_sku = EXCLUDED.external_sku,
                        channel_type = EXCLUDED.channel_type,
                        trigger_source = EXCLUDED.trigger_source,
                        next_retry_at = now(),
                        updated_at = now()
                    """
                ),
                {
                    "tid": tenant_id,
                    "vid": variant_id,
                    "mid": row.mapping_id,
                    "caid": row.channel_account_id,
                    "ctype": str(row.channel_type),
                    "ext_sku": str(row.external_sku),
                    "qty": sellable_qty,
                    "tsrc": trigger_source,
                },
            )
            enqueued += 1
        return enqueued

    @staticmethod
    def _normalize_sorted_items(items: list[tuple[UUID, int]]) -> list[tuple[UUID, int]]:
        merged: dict[UUID, int] = defaultdict(int)
        for variant_id, qty in items:
            if qty <= 0:
                raise ValueError(f"Inventory quantity must be positive, got {qty}")
            merged[variant_id] += qty
        # Sort ascending by variant_id string to enforce global lock acquisition order
        return sorted(merged.items(), key=lambda pair: str(pair[0]))

    async def inbound_stock(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        variant_id: UUID,
        qty: int,
        reference_id: UUID,
        idempotency_key: str,
        event_type: InventoryEventType = InventoryEventType.INBOUND_PO,
    ) -> int | None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        async with session.begin_nested():
            ledger_res = await session.execute(
                text(
                    """
                    INSERT INTO inventory_ledger (
                        tenant_id, warehouse_id, variant_id, event_type,
                        qty_delta_on_hand, qty_delta_reserved,
                        reference_type, reference_id, idempotency_key
                    ) VALUES (
                        :tid, :wid, :vid, :etype, :d_hand, 0, 'po', :ref_id, :ikey
                    )
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": variant_id,
                    "etype": event_type.value,
                    "d_hand": qty,
                    "ref_id": reference_id,
                    "ikey": idempotency_key,
                },
            )
            ledger_id = ledger_res.scalar_one_or_none()
            if ledger_id is None:
                return None

            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (
                        tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty
                    )
                    VALUES (:tid, :wid, :vid, :qty, 0)
                    ON CONFLICT (tenant_id, warehouse_id, variant_id)
                    DO UPDATE SET
                        on_hand_qty = inventory_balances.on_hand_qty + EXCLUDED.on_hand_qty
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
            )
            await self._enqueue_sync_jobs_for_variant(
                session, tenant_id, variant_id, trigger_source="inbound_stock"
            )
            return int(ledger_id)

    async def adjust_stocktake(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        variant_id: UUID,
        qty_delta: int,
        reference_id: UUID,
        idempotency_key: str,
    ) -> int | None:
        """Apply a signed ledger movement without consuming reserved stock."""
        if not -1_000_000_000 <= qty_delta <= 1_000_000_000 or qty_delta == 0:
            raise ValueError("Stocktake delta must be nonzero and within the supported range")
        params = {
            "tid": tenant_id,
            "wid": warehouse_id,
            "vid": variant_id,
            "qty": qty_delta,
            "ref": reference_id,
            "key": idempotency_key,
        }
        async with session.begin_nested():
            ledger_id = await session.scalar(
                text("""
                INSERT INTO inventory_ledger (
                    tenant_id, warehouse_id, variant_id, event_type, qty_delta_on_hand,
                    qty_delta_reserved, reference_type, reference_id, idempotency_key
                ) VALUES (:tid, :wid, :vid, 'stocktake_adjust', :qty, 0, 'stocktake', :ref, :key)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id
            """),
                params,
            )
            if ledger_id is None:
                return None
            if qty_delta > 0:
                quantity = await session.scalar(
                    text("""
                    INSERT INTO inventory_balances (
                        tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty
                    ) VALUES (:tid, :wid, :vid, :qty, 0)
                    ON CONFLICT (tenant_id, warehouse_id, variant_id) DO UPDATE
                    SET on_hand_qty = inventory_balances.on_hand_qty + EXCLUDED.on_hand_qty
                    WHERE inventory_balances.on_hand_qty <= 2147483647 - EXCLUDED.on_hand_qty
                    RETURNING on_hand_qty
                """),
                    params,
                )
            else:
                quantity = await session.scalar(
                    text("""
                    UPDATE inventory_balances SET on_hand_qty = on_hand_qty + :qty
                    WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                      AND on_hand_qty - reserved_qty >= -CAST(:qty AS integer)
                    RETURNING on_hand_qty
                """),
                    params,
                )
            if quantity is None:
                raise AppError(
                    slug="stocktake-out-of-range",
                    status_code=409,
                    code="ERR_STOCKTAKE_OUT_OF_RANGE",
                    title="Invalid stocktake quantity",
                    detail="The count would use reserved units or exceed inventory limits.",
                )
            await self._enqueue_sync_jobs_for_variant(
                session, tenant_id, variant_id, trigger_source="stocktake_adjust"
            )
            return int(ledger_id)

    async def reserve_for_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        order_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                # 1. Lock balance row in deterministic ascending variant_id order
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                # 2. Insert immutable ledger record idempotently
                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved,
                        reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'reserve_order',
                                0, :qty, 'order', :oid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "qty": qty,
                            "oid": order_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                available = (
                    0 if bal_row is None else int(bal_row.on_hand_qty - bal_row.reserved_qty)
                )
                if available < qty:
                    raise InsufficientStockError(
                        variant_id=variant_id, requested=qty, available=available
                    )

                updated = (
                    await session.execute(
                        text(
                            """
                            UPDATE inventory_balances
                            SET reserved_qty = reserved_qty + :qty
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                              AND (on_hand_qty - reserved_qty) >= :qty
                            RETURNING reserved_qty
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                    )
                ).scalar_one_or_none()
                if updated is None:
                    raise InsufficientStockError(
                        variant_id=variant_id, requested=qty, available=available
                    )

                ledger_ids.append(int(ledger_id))
                await self._enqueue_sync_jobs_for_variant(
                    session, tenant_id, variant_id, trigger_source="order_reserve"
                )

        return ledger_ids

    async def release_reservation(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        order_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved,
                        reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'release_cancel',
                                0, :neg_qty, 'order', :oid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "neg_qty": -qty,
                            "oid": order_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                if bal_row is None or int(bal_row.reserved_qty) < qty:
                    raise AppError(
                        slug="invalid-reservation-release",
                        status_code=409,
                        code="ERR_INVALID_RESERVATION_RELEASE",
                        title="Invalid Reservation Release",
                        detail=f"Cannot release {qty} reserved units for variant '{variant_id}'.",
                    )

                await session.execute(
                    text(
                        """
                        UPDATE inventory_balances
                        SET reserved_qty = reserved_qty - :qty
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                          AND reserved_qty >= :qty
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                )
                ledger_ids.append(int(ledger_id))
                await self._enqueue_sync_jobs_for_variant(
                    session, tenant_id, variant_id, trigger_source="order_release"
                )

        return ledger_ids

    async def deduct_shipped(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        shipment_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved,
                        reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'deduct_shipped',
                                :neg_qty, :neg_qty, 'shipment', :sid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "neg_qty": -qty,
                            "sid": shipment_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                if (
                    bal_row is None
                    or int(bal_row.on_hand_qty) < qty
                    or int(bal_row.reserved_qty) < qty
                ):
                    raise InsufficientStockError(
                        variant_id=variant_id,
                        requested=qty,
                        available=0 if bal_row is None else int(bal_row.reserved_qty),
                    )

                await session.execute(
                    text(
                        """
                        UPDATE inventory_balances
                        SET on_hand_qty = on_hand_qty - :qty,
                            reserved_qty = reserved_qty - :qty
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                          AND on_hand_qty >= :qty AND reserved_qty >= :qty
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                )
                ledger_ids.append(int(ledger_id))
                await self._enqueue_sync_jobs_for_variant(
                    session, tenant_id, variant_id, trigger_source="order_shipped"
                )

        return ledger_ids

    async def get_balance(
        self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, variant_id: UUID
    ) -> InventoryBalanceRead:
        row = (
            await session.execute(
                text(
                    """
                    SELECT on_hand_qty, reserved_qty
                    FROM inventory_balances
                    WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
            )
        ).one_or_none()
        on_hand = 0 if row is None else int(row.on_hand_qty)
        reserved = 0 if row is None else int(row.reserved_qty)
        return InventoryBalanceRead(
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            on_hand_qty=on_hand,
            reserved_qty=reserved,
            available_qty=on_hand - reserved,
        )

    async def list_balances(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[InventoryBalanceItemRead]:
        filter_sql = " AND ib.variant_id = :vid" if variant_id is not None else ""
        params: dict[str, object] = {"tid": tenant_id, "lim": limit, "off": offset}
        if variant_id is not None:
            params["vid"] = variant_id

        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM inventory_balances ib "
                        f"WHERE ib.tenant_id = :tid{filter_sql}"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT ib.warehouse_id, ib.variant_id,
                           COALESCE(pv.sku, ib.variant_id::text) AS sku,
                           ib.on_hand_qty, ib.reserved_qty
                    FROM inventory_balances ib
                    LEFT JOIN product_variants pv
                        ON pv.id = ib.variant_id AND pv.tenant_id = ib.tenant_id
                    WHERE ib.tenant_id = :tid{filter_sql}
                    ORDER BY sku ASC, ib.warehouse_id ASC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).all()
        items = [
            InventoryBalanceItemRead(
                warehouse_id=r.warehouse_id,
                variant_id=r.variant_id,
                sku=str(r.sku),
                on_hand_qty=int(r.on_hand_qty),
                reserved_qty=int(r.reserved_qty),
                available_qty=int(r.on_hand_qty) - int(r.reserved_qty),
            )
            for r in rows
        ]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def list_ledger(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[InventoryLedgerRead]:
        filter_sql = " AND variant_id = :vid" if variant_id is not None else ""
        params: dict[str, object] = {"tid": tenant_id, "lim": limit, "off": offset}
        if variant_id is not None:
            params["vid"] = variant_id

        total = int(
            (
                await session.execute(
                    text(
                        f"SELECT COUNT(*) FROM inventory_ledger WHERE tenant_id = :tid{filter_sql}"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, variant_id, event_type, qty_delta_on_hand, qty_delta_reserved,
                           reference_type, reference_id, idempotency_key, created_at
                    FROM inventory_ledger
                    WHERE tenant_id = :tid{filter_sql}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).all()
        items = [
            InventoryLedgerRead(
                id=int(r.id),
                variant_id=r.variant_id,
                event_type=str(r.event_type),
                qty_delta_on_hand=int(r.qty_delta_on_hand),
                qty_delta_reserved=int(r.qty_delta_reserved),
                reference_type=str(r.reference_type),
                reference_id=r.reference_id,
                idempotency_key=str(r.idempotency_key),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )
